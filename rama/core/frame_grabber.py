import logging
import threading
import time

logger = logging.getLogger(__name__)


class FrameGrabber:
    """
    Lee frames de la cámara en un hilo propio y guarda solo el más reciente.

    Con RTSP, si el pipeline procesa más lento de lo que la cámara envía,
    OpenCV va acumulando frames y el retraso crece hasta varios segundos.
    Este hilo vacía el stream continuamente, así el pipeline siempre trabaja
    sobre el último frame y los intermedios se descartan.
    """

    def __init__(self, camera, on_frame=None, on_read=None):
        """
        Args:
            camera (Camera): Cámara ya abierta.
            on_frame (callable|None): Se llama con cada frame leído
                                      (p.ej. para el stream del dashboard).
            on_read (callable|None): Se llama con (ok, read_ms) en cada lectura
                                     (telemetría).
        """
        self.camera = camera
        self.on_frame = on_frame
        self.on_read = on_read
        self._cond = threading.Condition()
        self._frame = None
        self._ts = 0.0
        self._seq = 0
        self.running = False
        self._thread = None

    def start(self):
        """Arranca el hilo lector."""
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hilo-camara")
        self._thread.start()
        logger.info("FrameGrabber arrancado.")

    def _loop(self):
        while self.running:
            t0 = time.perf_counter()
            frame = self.camera.read_frame()
            read_ms = (time.perf_counter() - t0) * 1000

            if self.on_read:
                self.on_read(frame is not None, read_ms)

            if frame is None:
                continue

            with self._cond:
                self._frame = frame
                self._ts = time.time()
                self._seq += 1
                self._cond.notify_all()

            if self.on_frame:
                try:
                    self.on_frame(frame)
                except Exception as e:
                    logger.error(f"Error en on_frame: {e}")

    def get_latest(self, last_seq, timeout=1.0):
        """
        Devuelve el frame más reciente posterior a last_seq.

        Args:
            last_seq (int): Secuencia del último frame procesado.
            timeout (float): Segundos máximos de espera.

        Returns:
            tuple|None: (frame, ts_captura, seq) o None si no llegó ninguno nuevo.
        """
        with self._cond:
            self._cond.wait_for(lambda: self._seq > last_seq or not self.running, timeout)
            if self._seq <= last_seq:
                return None
            return self._frame, self._ts, self._seq

    def ultimo(self):
        """Último frame leído (o None), sin esperar. Para el editor de ROI."""
        with self._cond:
            return self._frame

    def stop(self):
        """Para el hilo lector."""
        self.running = False
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=3)
