import logging
import queue
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
        hasta que se llame a stop(). En cada frame ejecuta detección,
        OCR y validación en orden. Si el validator acepta una matrícula
        la persiste via JsonStore y publica el evento en EQbroker.

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
            frame = self.camera.read_frame()
            self.frame_count += 1

            if frame is None:
                logger.warning("Frame nulo recibido, saltando.")
                continue

            # Frames saltados por FRAME_SKIP — solo se mandan al stream sin procesar
            if self.frame_count % self.frame_skip != 0:
                self._send_frame_to_stream(frame)
                continue

            crop_frame, best_confidence_detector, bbox = self.detector.detect(frame=frame)

            vehiculo_presente = crop_frame is not None

            if not vehiculo_presente:
                self.validator.validate(None, 0.0, vehiculo_presente=False)
                self._send_frame_to_stream(frame)
                continue

            logger.debug(f"Matrícula detectada con confianza {best_confidence_detector:.2f}")

            # Dibujar bbox en el frame antes de mandarlo al stream
            x1, y1, x2, y2 = bbox
            annotated = frame.copy()
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 136), 2)

            text, best_confidence_ocr = self.ocr.run_ocr(crop_frame)

            if text is None:
                logger.debug("OCR no devolvió lectura válida.")
                self._send_frame_to_stream(annotated)
                continue

            logger.debug(f"OCR: '{text}' con confianza {best_confidence_ocr:.2f}")

            # Dibujar texto OCR encima del bbox
            cv2.putText(
                annotated, f"{text} {best_confidence_ocr:.2f}",
                (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (0, 255, 136), 2, cv2.LINE_AA
            )

            matricula = self.validator.validate(text, best_confidence_ocr, vehiculo_presente=True)

            if matricula:
                logger.info(f"Matrícula aceptada: {matricula}")

                fingerprint = self.fingerprint.get_fingerprint(frame)
                self.store.save(matricula, frame, crop_frame, fingerprint)

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

            self._send_frame_to_stream(annotated)

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