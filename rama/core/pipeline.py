import logging
import queue
import time
import cv2
from core.camera import Camera
from core.frame_grabber import FrameGrabber
from config.config import config
from datetime import datetime
if config['detection']['backend'] == 'coral':
    from core.detector_coral import DetectorCoral as Detector
else:
    from core.detector_yolo import DetectorYOLO as Detector
from core.ocr import OCR
from core.validator import Validator
from storage.json_store import JsonStore
from core.fingerprint import Fingerprint
from core.telemetry import telemetry


logger = logging.getLogger(__name__)

# location.country (ISO de 2 letras) → código de país de plate_templates.py
PAISES_PLANTILLAS = {"ES": "ESP", "IT": "ITA", "PT": "POR", "MX": "MEX", "RO": "RUM"}


class Pipeline:
    """
    Orquestador principal del sistema RAMA.

    Coordina el flujo completo de detección y lectura de matrículas:
    captura de frames → detección → OCR → validación → persistencia.
    No contiene lógica propia, delega en cada módulo especializado.

    La captura va en su propio hilo (FrameGrabber) y el pipeline procesa
    siempre el frame más reciente: si la inferencia es más lenta que la
    cámara se descartan frames intermedios en lugar de acumular retraso.
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
        self.store = JsonStore()
        self.fingerprint = Fingerprint()
        self.queue = cola
        self.cola_stream = cola_stream
        self.running = False
        self.grabber = None

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
        Redimensiona el frame y lo mete en la cola del stream.

        Si la cola está llena descarta el frame silenciosamente —
        es normal bajo carga alta o si el dashboard no está conectado.

        Args:
            frame (numpy.ndarray): Frame en formato BGR a enviar al stream.
        """
        try:
            frame_small = cv2.resize(frame, self.stream_size)
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
        Cada frame procesado pasa por _process_frame (detección → OCR →
        validación → persistencia) y sus tiempos y resultados se registran
        en la telemetría para el dashboard de depuración (/debug).
        """
        if not self.camera.open_camera():
            logger.error("No se pudo abrir la cámara. Abortando pipeline.")
            return

        self.grabber = FrameGrabber(
            self.camera,
            on_frame=self._stream_raw_frame,
            on_read=telemetry.frame_leido,
        )
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

            if ev.get("detected"):
                self._last_annotated = time.time()
                self._send_frame_to_stream(annotated)

    def _process_frame(self, frame, ev):
        """
        Ejecuta detector → OCR → validator → persistencia sobre un frame.

        Va rellenando `ev` con los tiempos de cada etapa (ms) y los
        resultados intermedios para la telemetría.

        Args:
            frame (numpy.ndarray): Frame BGR a procesar.
            ev (dict): Evento de telemetría del frame, se modifica in situ.

        Returns:
            tuple: (frame anotado, recorte de la matrícula o None)
        """
        t = time.perf_counter()
        crop_frame, best_confidence_detector, bbox = self.detector.detect(frame=frame)
        ev["t_detect_ms"] = round((time.perf_counter() - t) * 1000, 1)

        vehiculo_presente = crop_frame is not None and crop_frame.size > 0
        ev["detected"] = vehiculo_presente

        if not vehiculo_presente:
            self.validator.validate(None, 0.0, vehiculo_presente=False)
            return frame, None

        logger.debug(f"Matrícula detectada con confianza {best_confidence_detector:.2f}")
        ev["det_conf"] = round(float(best_confidence_detector), 3)
        ev["bbox"] = [int(v) for v in bbox]
        ev["crop_wh"] = [crop_frame.shape[1], crop_frame.shape[0]]

        # Dibujar bbox en el frame antes de mandarlo al stream
        x1, y1, x2, y2 = ev["bbox"]
        annotated = frame.copy()
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 136), 2)

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
                0.7, (0, 255, 136), 2, cv2.LINE_AA
            )
        else:
            logger.debug("OCR no devolvió lectura válida.")

        # El validator también recibe las detecciones sin texto: así sabe
        # que el vehículo sigue presente y no cuenta el frame como perdido
        t = time.perf_counter()
        self.validator.last_decision = None
        matricula = self.validator.validate(text, best_confidence_ocr, vehiculo_presente=True)
        ev["t_validate_ms"] = round((time.perf_counter() - t) * 1000, 2)
        ev["validator"] = self.validator.last_decision

        if matricula:
            logger.info(f"Matrícula aceptada: {matricula}")

            t = time.perf_counter()
            try:
                fingerprint = self.fingerprint.get_fingerprint(frame, plate_y1=y1)
            except Exception as e:
                # Que un fallo del color no tumbe el pipeline en las pruebas de campo
                logger.exception(f"Error calculando fingerprint: {e}")
                fingerprint = None
            ev["fingerprint"] = fingerprint
            self.store.save(matricula, frame, crop_frame, fingerprint)
            ev["t_store_ms"] = round((time.perf_counter() - t) * 1000, 1)

            try:
                evento = {
                    "license_plate": matricula,
                    "confidence": best_confidence_ocr,
                    "timestamp": datetime.now().isoformat(),
                    "fingerprint": None
                }
                self.queue.put_nowait(evento)
            except queue.Full:
                logger.warning("Cola EQbroker llena — evento descartado.")

        return annotated, crop_frame

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
