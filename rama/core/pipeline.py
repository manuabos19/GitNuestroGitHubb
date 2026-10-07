import logging
import queue
import time
import cv2
from core.camera import Camera
from core.frame_grabber import FrameGrabber
from config.config import config
from datetime import datetime
if config['detection']['backend'] == 'coral':
    from core.detector_coral_multi import DetectorCoralMulti as Detector
else:
    from core.detector_yolo import DetectorYOLO as Detector
from core.ocr import OCR
from core.validator import Validator
from core.roi import Roi
from core.tracker import PlateTracker
from storage.json_store import JsonStore
from core.telemetry import telemetry


logger = logging.getLogger(__name__)

# location.country (ISO de 2 letras) → código de país de plate_templates.py
PAISES_PLANTILLAS = {"ES": "ESP", "IT": "ITA", "PT": "POR", "MX": "MEX", "RO": "RUM"}

# El fingerprint (color del vehículo) está desactivado por ahora. JsonStore
# sigue recibiendo un dict con la misma forma para no romper lo que lo lea.
FINGERPRINT_DESACTIVADO = {"color": None, "is_color_frame": None}

COLOR_SEGUIDA = (0, 255, 136)
COLOR_IGNORADA = (0, 140, 255)


class Pipeline:
    """
    Orquestador principal del sistema RAMA.

    Coordina el flujo completo de detección y lectura de matrículas:
    captura de frames → (ROI) → detección → (seguimiento) → OCR →
    validación → persistencia. No contiene lógica propia, delega en cada
    módulo especializado.

    La captura va en su propio hilo (FrameGrabber) y el pipeline procesa
    siempre el frame más reciente: si la inferencia es más lenta que la
    cámara se descartan frames intermedios en lugar de acumular retraso.

    Dos opciones activables en caliente (settings.yaml o dashboard /debug):
      - detection.roi:    solo se buscan matrículas dentro de un polígono.
      - tracking.enabled: se sigue a un único vehículo hasta que se va; el
                          resto de matrículas se ignoran mientras tanto.
    """

    def __init__(self, cola: queue.Queue, cola_stream: queue.Queue):
        """
        Inicializa todos los módulos del pipeline y la flag de control.

        Args:
            cola (queue.Queue): Cola compartida con el hilo asyncio para
                                publicar detecciones en EQbroker.
            cola_stream (queue.Queue): Cola compartida con el WebSocket para
                                       enviar frames al dashboard en tiempo real.
        """
        pais = config.get('location', {}).get('country')
        home_country = PAISES_PLANTILLAS.get(pais, pais)

        self.camera = Camera()
        self.detector = Detector()
        self.ocr = OCR(home_country=home_country)
        self.validator = Validator()
        self.roi = Roi()
        self.tracker = PlateTracker()
        self.store = JsonStore()
        self.queue = cola
        self.cola_stream = cola_stream
        self.running = False
        self.grabber = None

        # Estado del vehículo seguido (solo con tracking)
        self._track_leido = False
        self._track_mejor = None  # (frame, crop, det_conf, ocr_conf) para decidir al salir

        # Stream del dashboard: solo visualización, no afecta a la detección
        stream_cfg = config.get('stream', {})
        self.stream_size = (stream_cfg.get('width', 320), stream_cfg.get('height', 180))
        self.stream_interval = 1.0 / stream_cfg.get('fps', 10)
        self._last_stream = 0.0
        self._last_annotated = 0.0

        telemetry.registrar_colas(eqbroker=cola, stream=cola_stream)
        logger.info(f"Pipeline inicializado (OCR home_country={home_country}).")

    def _send_frame_to_stream(self, frame):
        """
        Redimensiona el frame, dibuja la ROI y lo mete en la cola del stream.

        Si la cola está llena descarta el frame silenciosamente —
        es normal bajo carga alta o si el dashboard no está conectado.

        Args:
            frame (numpy.ndarray): Frame en formato BGR a enviar al stream.
        """
        try:
            frame_small = cv2.resize(frame, self.stream_size)
            self.roi.dibujar(frame_small)
            self.cola_stream.put_nowait(frame_small)
            self._last_stream = time.time()
        except queue.Full:
            pass

    def _stream_raw_frame(self, frame):
        """
        Envía al stream los frames que lee la cámara, limitado a stream.fps.

        Si hace poco se mandó un frame anotado (con bbox) no se manda el
        crudo, para que el recuadro no parpadee en el dashboard.
        """
        ahora = time.time()
        if ahora - self._last_stream < self.stream_interval:
            return
        if ahora - self._last_annotated < 0.5:
            return
        self._send_frame_to_stream(frame)

    def start(self):
        """
        Inicia el bucle principal de detección.

        Abre la conexión con la cámara, arranca el hilo lector y procesa
        el frame más reciente en cada vuelta hasta que se llame a stop().
        Cada frame procesado pasa por _process_frame y sus tiempos y
        resultados se registran en la telemetría para el dashboard (/debug).
        """
        if not self.camera.open_camera():
            logger.error("No se pudo abrir la cámara. Abortando pipeline.")
            return

        self.grabber = FrameGrabber(
            self.camera,
            on_frame=self._stream_raw_frame,
            on_read=telemetry.frame_leido,
        )
        telemetry.grabber = self.grabber
        self.grabber.start()

        self.running = True
        logger.info("Pipeline iniciado. Procesando frames...")

        last_seq = 0
        while self.running:
            item = self.grabber.get_latest(last_seq, timeout=1.0)
            if item is None:
                continue

            frame, ts_captura, seq = item
            ev = {
                "frame_id": seq,
                "ts": ts_captura,
                "frame_wh": [frame.shape[1], frame.shape[0]],
                # Frames que llegaron mientras se procesaba el anterior
                "dropped": seq - last_seq - 1 if last_seq else 0,
                "t_wait_ms": round((time.time() - ts_captura) * 1000, 1),
            }
            last_seq = seq

            t_inicio = time.perf_counter()
            annotated, crop_frame = self._process_frame(frame, ev)
            ev["t_total_ms"] = round((time.perf_counter() - t_inicio) * 1000, 1)
            # Retraso real: desde que la cámara entregó el frame hasta tener resultado
            ev["age_ms"] = round((time.time() - ts_captura) * 1000, 1)

            telemetry.frame_procesado(ev, crop_frame, annotated)

            if ev.get("dets"):
                self._last_annotated = time.time()
                self._send_frame_to_stream(annotated)

    def _detectar(self, frame, ev):
        """
        Ejecuta el detector (dentro de la ROI si está activa).

        Returns:
            list[dict]: Detecciones válidas en coordenadas del frame completo.
        """
        if self.roi.activa():
            imagen, (ox, oy) = self.roi.recortar(frame)
        else:
            imagen, ox, oy = frame, 0, 0

        detecciones = [
            {"bbox": (x1 + ox, y1 + oy, x2 + ox, y2 + oy), "conf": d["conf"]}
            for d in self.detector.detect_all(imagen)
            for x1, y1, x2, y2 in [d["bbox"]]
        ]

        ev["roi"] = self.roi.activa()
        if ev["roi"]:
            dentro = [d for d in detecciones if self.roi.contiene(d["bbox"])]
            ev["ignored_roi"] = len(detecciones) - len(dentro)
            detecciones = dentro

        ev["dets"] = [{"bbox": [int(v) for v in d["bbox"]], "conf": round(d["conf"], 3)}
                      for d in detecciones[:8]]
        return detecciones

    def _process_frame(self, frame, ev):
        """
        Ejecuta ROI → detector → seguimiento → OCR → validator → persistencia.

        Va rellenando `ev` con los tiempos de cada etapa (ms) y los
        resultados intermedios para la telemetría.

        Args:
            frame (numpy.ndarray): Frame BGR a procesar.
            ev (dict): Evento de telemetría del frame, se modifica in situ.

        Returns:
            tuple: (frame anotado, recorte de la matrícula o None)
        """
        tracking_cfg = config.get('tracking') or {}
        tracking = bool(tracking_cfg.get('enabled'))

        t = time.perf_counter()
        detecciones = self._detectar(frame, ev)
        ev["t_detect_ms"] = round((time.perf_counter() - t) * 1000, 1)

        # Elegir qué matrícula se lee
        if tracking:
            det, info = self.tracker.update(detecciones)
            ev["track"] = info
            if info["ended"] is not None:
                # El vehículo seguido se ha ido (o se cambia a uno más cercano)
                self._fin_de_vehiculo(ev, tracking_cfg)
            if info["new"]:
                # Vehículo nuevo: nada del anterior debe contar en el voting
                self.validator.reset()
                self._track_leido = False
                self._track_mejor = None
        else:
            if self.tracker.track is not None:
                # Se acaba de desactivar el seguimiento desde el dashboard
                self.tracker.reset()
                self._track_leido = False
                self._track_mejor = None
            det = detecciones[0] if detecciones else None

        annotated = frame.copy() if detecciones else frame
        for d in detecciones:
            if d is not det:
                x1, y1, x2, y2 = d["bbox"]
                cv2.rectangle(annotated, (x1, y1), (x2, y2), COLOR_IGNORADA, 2)

        ev["detected"] = det is not None
        if det is None:
            if not tracking:
                self.validator.validate(None, 0.0, vehiculo_presente=False)
            return annotated, None

        x1, y1, x2, y2 = det["bbox"]
        crop_frame = frame[y1:y2, x1:x2]
        logger.debug(f"Matrícula detectada con confianza {det['conf']:.2f}")
        ev["det_conf"] = round(float(det["conf"]), 3)
        ev["bbox"] = [int(x1), int(y1), int(x2), int(y2)]
        ev["crop_wh"] = [crop_frame.shape[1], crop_frame.shape[0]]
        cv2.rectangle(annotated, (x1, y1), (x2, y2), COLOR_SEGUIDA, 2)

        # Vehículo ya leído: no se vuelve a pasar el OCR hasta que se vaya
        if tracking and self._track_leido and tracking_cfg.get('skip_ocr_after_read', True):
            ev["ocr_skipped"] = True
            ev["validator"] = {"decision": "ya_leida"}
            return annotated, crop_frame

        t = time.perf_counter()
        text, best_confidence_ocr = self.ocr.run_ocr(crop_frame)
        ev["t_ocr_ms"] = round((time.perf_counter() - t) * 1000, 1)
        ev["ocr_text"] = text
        ev["ocr_conf"] = round(float(best_confidence_ocr), 3)

        if text is not None:
            logger.debug(f"OCR: '{text}' con confianza {best_confidence_ocr:.2f}")
            # Dibujar texto OCR encima del bbox
            cv2.putText(
                annotated, f"{text} {best_confidence_ocr:.2f}",
                (x1, max(20, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, COLOR_SEGUIDA, 2, cv2.LINE_AA
            )
        else:
            logger.debug("OCR no devolvió lectura válida.")

        if tracking and (self._track_mejor is None or det["conf"] >= self._track_mejor[2]):
            self._track_mejor = (frame, crop_frame, det["conf"], best_confidence_ocr)

        # El validator también recibe las detecciones sin texto: así sabe
        # que el vehículo sigue presente y no cuenta el frame como perdido
        t = time.perf_counter()
        self.validator.last_decision = None
        matricula = self.validator.validate(text, best_confidence_ocr, vehiculo_presente=True)
        ev["t_validate_ms"] = round((time.perf_counter() - t) * 1000, 2)
        ev["validator"] = self.validator.last_decision

        if matricula:
            self._publicar(matricula, frame, crop_frame, best_confidence_ocr, ev)
            if tracking:
                self._track_leido = True

        return annotated, crop_frame

    def _fin_de_vehiculo(self, ev, tracking_cfg):
        """
        El vehículo seguido ha salido. Si no se llegó a leer y está activado
        tracking.decide_on_exit, se decide con los votos acumulados.
        """
        if not self._track_leido and tracking_cfg.get('decide_on_exit', True):
            self.validator.last_decision = None
            matricula = self.validator.finalize(tracking_cfg.get('min_frames_on_exit', 2))
            # Aparte de ev["validator"]: en el mismo frame puede empezar otro vehículo
            ev["exit_validator"] = self.validator.last_decision
            if matricula and self._track_mejor:
                frame, crop, _, ocr_conf = self._track_mejor
                self._publicar(matricula, frame, crop, ocr_conf, ev)
        else:
            self.validator.reset()
        self._track_leido = False
        self._track_mejor = None

    def _publicar(self, matricula, frame, crop_frame, confianza, ev):
        """Guarda la lectura aceptada y la manda a EQbroker."""
        logger.info(f"Matrícula aceptada: {matricula}")

        t = time.perf_counter()
        self.store.save(matricula, frame, crop_frame, FINGERPRINT_DESACTIVADO)
        ev["t_store_ms"] = round((time.perf_counter() - t) * 1000, 1)

        try:
            evento = {
                "license_plate": matricula,
                "confidence": confianza,
                "timestamp": datetime.now().isoformat(),
                "fingerprint": None
            }
            self.queue.put_nowait(evento)
        except queue.Full:
            logger.warning("Cola EQbroker llena — evento descartado.")

    def stop(self):
        """
        Detiene el pipeline limpiamente.

        Para el bucle principal y el hilo lector, cierra la conexión con
        la cámara y resetea el estado del validator.
        """
        self.running = False
        if self.grabber:
            self.grabber.stop()
        self.camera.close_camera()
        self.validator.reset()
        logger.info("Pipeline detenido.")
