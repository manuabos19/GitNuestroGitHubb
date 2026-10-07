import logging
import queue
import time
import cv2
from core.camera import Camera
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


class Pipeline:
    """
    Orquestador principal del sistema RAMA.

    Coordina el flujo completo de detección y lectura de matrículas:
    captura de frames → detección → OCR → validación → persistencia.
    No contiene lógica propia, delega en cada módulo especializado.
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
        self.camera = Camera()
        self.detector = Detector()
        self.ocr = OCR()
        self.validator = Validator()
        self.store = JsonStore()
        self.fingerprint = Fingerprint()
        self.queue = cola
        self.cola_stream = cola_stream
        self.running = False
        self.frame_skip = 3
        self.frame_count = 0
        telemetry.frame_skip = self.frame_skip
        telemetry.registrar_colas(eqbroker=cola, stream=cola_stream)
        logger.info("Pipeline inicializado.")

    def _send_frame_to_stream(self, frame):
        """
        Redimensiona el frame y lo mete en la cola del stream.

        Si la cola está llena descarta el frame silenciosamente —
        es normal bajo carga alta o si el dashboard no está conectado.

        Args:
            frame (numpy.ndarray): Frame en formato BGR a enviar al stream.
        """
        try:
            frame_small = cv2.resize(frame, (320, 180))
            self.cola_stream.put_nowait(frame_small)
        except queue.Full:
            pass

    def start(self):
        """
        Inicia el bucle principal de detección.

        Abre la conexión con la cámara y procesa frames continuamente
        hasta que se llame a stop(). Cada frame procesado pasa por
        _process_frame (detección → OCR → validación → persistencia) y
        sus tiempos y resultados se registran en la telemetría para el
        dashboard de depuración (/debug).

        Los frames saltados por FRAME_SKIP se envían igualmente al stream
        del dashboard sin pasar por detector ni OCR. Los frames procesados
        se envían con el bounding box dibujado si hay detección.
        """
        if not self.camera.open_camera():
            logger.error("No se pudo abrir la cámara. Abortando pipeline.")
            return

        self.running = True
        logger.info("Pipeline iniciado. Procesando frames...")

        while self.running:
            t0 = time.perf_counter()
            frame = self.camera.read_frame()
            read_ms = (time.perf_counter() - t0) * 1000
            self.frame_count += 1
            telemetry.frame_leido(frame is not None, read_ms)

            if frame is None:
                logger.warning("Frame nulo recibido, saltando.")
                continue

            # Frames saltados por FRAME_SKIP — solo se mandan al stream sin procesar
            if self.frame_count % self.frame_skip != 0:
                self._send_frame_to_stream(frame)
                continue

            ev = {
                "frame_id": self.frame_count,
                "ts": time.time(),
                "frame_wh": [frame.shape[1], frame.shape[0]],
                "t_read_ms": round(read_ms, 1),
            }
            t_inicio = time.perf_counter()
            annotated, crop_frame = self._process_frame(frame, ev)
            ev["t_total_ms"] = round((time.perf_counter() - t_inicio) * 1000, 1)

            telemetry.frame_procesado(ev, crop_frame)
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
            tuple: (frame a enviar al stream, recorte de la matrícula o None)
        """
        t = time.perf_counter()
        crop_frame, best_confidence_detector, bbox = self.detector.detect(frame=frame)
        ev["t_detect_ms"] = round((time.perf_counter() - t) * 1000, 1)

        vehiculo_presente = crop_frame is not None
        ev["detected"] = vehiculo_presente

        if not vehiculo_presente:
            self.validator.validate(None, 0.0, vehiculo_presente=False)
            return frame, None

        logger.debug(f"Matrícula detectada con confianza {best_confidence_detector:.2f}")
        ev["det_conf"] = round(float(best_confidence_detector), 3)
        ev["bbox"] = [int(v) for v in bbox]
        ev["crop_wh"] = [crop_frame.shape[1], crop_frame.shape[0]]

        # Dibujar bbox en el frame antes de mandarlo al stream
        x1, y1, x2, y2 = bbox
        annotated = frame.copy()
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 136), 2)

        t = time.perf_counter()
        text, best_confidence_ocr = self.ocr.run_ocr(crop_frame)
        ev["t_ocr_ms"] = round((time.perf_counter() - t) * 1000, 1)
        ev["ocr_text"] = text
        ev["ocr_conf"] = round(float(best_confidence_ocr), 3)

        if text is None:
            logger.debug("OCR no devolvió lectura válida.")
            return annotated, crop_frame

        logger.debug(f"OCR: '{text}' con confianza {best_confidence_ocr:.2f}")

        # Dibujar texto OCR encima del bbox
        cv2.putText(
            annotated, f"{text} {best_confidence_ocr:.2f}",
            (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX,
            0.7, (0, 255, 136), 2, cv2.LINE_AA
        )

        t = time.perf_counter()
        self.validator.last_decision = None
        matricula = self.validator.validate(text, best_confidence_ocr, vehiculo_presente=True)
        ev["t_validate_ms"] = round((time.perf_counter() - t) * 1000, 2)
        ev["validator"] = self.validator.last_decision

        if matricula:
            logger.info(f"Matrícula aceptada: {matricula}")

            t = time.perf_counter()
            try:
                fingerprint = self.fingerprint.get_fingerprint(frame)
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

        Para el bucle principal, cierra la conexión con la cámara
        y resetea el estado del validator.
        """
        self.running = False
        self.camera.close_camera()
        self.validator.reset()
        logger.info("Pipeline detenido.")