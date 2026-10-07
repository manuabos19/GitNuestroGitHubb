import cv2
import time
import logging
from config.config import config
from core import state

logger = logging.getLogger(__name__)


class Camera:
    """
    Gestiona la conexión y captura de frames desde una cámara IP via RTSP.
    
    Lee la configuración desde settings.yaml y construye la URL RTSP
    automáticamente. Incluye lógica de reconexión automática en caso
    de pérdida de señal.
    """

    def __init__(self):
        """
        Inicializa los parámetros de conexión desde la configuración
        y prepara el objeto de captura de OpenCV.
        """
        self.ip = config["camera"]["ip"]
        self.user = config["camera"]["user"]
        self.password = config["camera"]["password"]
        self.port = config["camera"]["port"]
        self.fps = config["camera"]["fps"]
        self.stream_path = config["camera"]["stream_path"]
        self.resolution_width = config["camera"]["resolution"]["width"]
        self.resolution_height = config["camera"]["resolution"]["height"]
        self.rtsp = f"rtsp://{self.user}:{self.password}@{self.ip}:{self.port}/{self.stream_path}"
        self.cap = None
        self.max_retries = 5
        self.retry_delay = 3

    def open_camera(self):
        """
        Abre la conexión con la cámara IP via RTSP.
        
        Intenta conectarse hasta max_retries veces esperando retry_delay
        segundos entre cada intento.
        
        Returns:
            bool: True si la conexión se estableció correctamente, False si
                  se agotaron todos los intentos.
        """
        for attempt in range(1, self.max_retries + 1):
            logger.info(f"Conectando a la cámara, intento {attempt}/{self.max_retries}...")
            self.cap = cv2.VideoCapture(self.rtsp)
            # Buffer mínimo: queremos el frame más reciente, no los acumulados
            # (no todos los backends lo respetan; FrameGrabber lo garantiza igualmente)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution_width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution_height)

            if self.cap.isOpened():
                logger.info("Conexión con la cámara establecida.")
                state.camera_connected = True
                return True

            logger.warning(f"No se pudo conectar. Reintentando en {self.retry_delay}s...")
            self.cap.release()
            time.sleep(self.retry_delay)

        logger.error("No se pudo conectar a la cámara tras todos los intentos.")
        state.camera_connected = False
        return False

    def read_frame(self):
        """
        Captura y devuelve el frame actual del stream.
        
        Si la cámara no está disponible intenta reconectar automáticamente
        antes de devolver None.
        
        Returns:
            numpy.ndarray: Frame capturado en formato BGR, o None si no
                           fue posible obtener un frame válido.
        """
        if self.cap is None or not self.cap.isOpened():
            logger.warning("Cámara no disponible, intentando reconectar...")
            if not self.open_camera():
                return None

        ret, frame = self.cap.read()

        if ret:
            return frame

        logger.warning("Error leyendo frame, intentando reconectar...")
        self.cap.release()
        self.open_camera()
        return None

    def close_camera(self):
        """
        Libera los recursos de la cámara y cierra la conexión RTSP.
        """
        if self.cap is not None:
            self.cap.release()
            self.cap = None
            state.camera_connected = False
            logger.info("Cámara cerrada.")