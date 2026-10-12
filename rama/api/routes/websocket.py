import asyncio
import logging
import queue
import time
import cv2
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from config.config import config
from core import state

router = APIRouter()
logger = logging.getLogger(__name__)


class CameraManager:
    """
    Gestiona las conexiones WebSocket activas del stream de cámara.

    Cada cliente puede pedir sus fps (/ws/camera?fps=3): el mosaico del
    panel general pide pocos y la vista de detalle más. Cada frame se
    codifica una sola vez y se envía solo a los clientes a los que les
    toca. El número de clientes y los fps más altos pedidos se publican
    en core.state para que la pipeline no prepare frames si nadie mira.
    """

    def __init__(self):
        # websocket -> {"fps": int, "interval": float, "last": float}
        self.connections: dict = {}

    @staticmethod
    def max_fps():
        """fps máximos del stream (stream.fps en settings.yaml, 10 por defecto)."""
        return max(1, int(config.get('stream', {}).get('fps', 10)))

    def _publicar_estado(self):
        state.stream_clients = len(self.connections)
        state.stream_fps = max((c["fps"] for c in self.connections.values()), default=0)

    async def connect(self, websocket: WebSocket, fps: int):
        """
        Acepta una nueva conexión WebSocket y la añade a la lista.

        Args:
            websocket (WebSocket): Conexión entrante del dashboard.
            fps (int): Frames por segundo que quiere recibir este cliente.
        """
        await websocket.accept()
        self.connections[websocket] = {"fps": fps, "interval": 1.0 / fps, "last": 0.0}
        self._publicar_estado()
        logger.info(f"Cliente conectado a /ws/camera ({fps} fps). Total: {len(self.connections)}")

    def disconnect(self, websocket: WebSocket):
        """
        Elimina una conexión de la lista de clientes activos.

        Args:
            websocket (WebSocket): Conexión a eliminar.
        """
        if websocket in self.connections:
            del self.connections[websocket]
            self._publicar_estado()
            logger.info(f"Cliente desconectado de /ws/camera. Total: {len(self.connections)}")

    def _pendientes(self):
        """Clientes a los que ya les toca recibir un frame según sus fps."""
        ahora = time.monotonic()
        # Margen del 10 % para que el redondeo de tiempos no salte frames
        return [ws for ws, c in self.connections.items() if ahora - c["last"] >= c["interval"] * 0.9]

    def alguien_espera(self):
        """True si a algún cliente le toca recibir frame (si no, no hace falta codificar)."""
        return bool(self._pendientes())

    async def broadcast(self, data: bytes):
        """
        Envía un frame en bytes a los clientes a los que les toca.

        Si el envío falla para algún cliente (conexión caída) lo
        elimina de la lista de conexiones activas.

        Args:
            data (bytes): Frame JPG codificado a enviar.
        """
        dead = []
        ahora = time.monotonic()
        for ws in self._pendientes():
            self.connections[ws]["last"] = ahora
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)


camera_manager = CameraManager()

# Cola de frames del pipeline, la registra main.py antes de arrancar uvicorn
_cola_stream = None


def set_stream_queue(cola_stream: queue.Queue):
    """
    Registra la cola de frames que alimenta el stream de cámara.

    stream_loop se lanza en el arranque de la app (event loop de uvicorn),
    el mismo loop que acepta las conexiones WebSocket. Enviar desde otro
    event loop (el de hilo-asyncio) no es seguro entre hilos.

    Args:
        cola_stream (queue.Queue): Cola compartida con el pipeline.
    """
    global _cola_stream
    _cola_stream = cola_stream


def start_stream():
    """Lanza stream_loop en el event loop actual (el de uvicorn)."""
    if _cola_stream is None:
        logger.warning("Sin cola de stream registrada — /ws/camera no enviará frames.")
        return
    asyncio.get_running_loop().create_task(stream_loop(_cola_stream))
    logger.info("Stream de cámara arrancado en el loop de uvicorn.")


@router.websocket('/ws/camera')
async def websocket_camera(websocket: WebSocket):
    """
    WebSocket para el stream de cámara en tiempo real.

    El frontend se conecta aquí para recibir los frames procesados
    por el pipeline (con bbox y OCR dibujados cuando hay detección).
    La conexión se mantiene abierta escuchando mensajes del cliente
    sin necesidad de responder — solo sirve para detectar desconexión.

    Parámetro opcional ?fps=N (1..stream.fps): frames por segundo que
    quiere este cliente. Por defecto, el máximo configurado.
    """
    maximo = CameraManager.max_fps()
    try:
        fps = int(websocket.query_params.get('fps', maximo))
    except ValueError:
        fps = maximo
    await camera_manager.connect(websocket, min(max(fps, 1), maximo))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("WS /camera desconectado por el cliente.")
    except Exception as e:
        logger.error(f"Error en WS /camera: {e}")
    finally:
        camera_manager.disconnect(websocket)


async def stream_loop(cola_stream: queue.Queue):
    """
    Corrutina que consume frames de cola_stream y los envía a todos
    los clientes conectados via CameraManager.

    Corre indefinidamente dentro del event loop de asyncio. Cuando
    la cola está vacía espera 50ms antes de volver a mirar. Cada
    frame se codifica a JPG una sola vez, y solo si a algún cliente
    le toca recibirlo; si no, se descarta sin codificar.

    Args:
        cola_stream (queue.Queue): Cola compartida con el pipeline,
                                    que mete frames redimensionados.
    """
    while True:
        try:
            frame = cola_stream.get_nowait()
            if not camera_manager.alguien_espera():
                continue
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            await camera_manager.broadcast(buffer.tobytes())
        except queue.Empty:
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Error en stream_loop: {e}")
            await asyncio.sleep(0.5)