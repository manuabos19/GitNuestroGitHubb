import asyncio
import logging
import queue
import cv2
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)


class CameraManager:
    """
    Gestiona las conexiones WebSocket activas del stream de cámara.

    Mantiene una lista de clientes conectados al dashboard y permite
    enviar un frame a todos ellos a la vez mediante broadcast.
    """

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """
        Acepta una nueva conexión WebSocket y la añade a la lista.

        Args:
            websocket (WebSocket): Conexión entrante del dashboard.
        """
        await websocket.accept()
        self.connections.append(websocket)
        logger.info(f"Cliente conectado a /ws/camera. Total: {len(self.connections)}")

    def disconnect(self, websocket: WebSocket):
        """
        Elimina una conexión de la lista de clientes activos.

        Args:
            websocket (WebSocket): Conexión a eliminar.
        """
        if websocket in self.connections:
            self.connections.remove(websocket)
            logger.info(f"Cliente desconectado de /ws/camera. Total: {len(self.connections)}")

    async def broadcast(self, data: bytes):
        """
        Envía un frame en bytes a todos los clientes conectados.

        Si el envío falla para algún cliente (conexión caída) lo
        elimina de la lista de conexiones activas.

        Args:
            data (bytes): Frame JPG codificado a enviar.
        """
        dead = []
        for ws in self.connections:
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
    """
    await camera_manager.connect(websocket)
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
    frame se codifica a JPG antes de enviarlo por WebSocket.

    Args:
        cola_stream (queue.Queue): Cola compartida con el pipeline,
                                    que mete frames redimensionados.
    """
    while True:
        try:
            frame = cola_stream.get_nowait()
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            await camera_manager.broadcast(buffer.tobytes())
        except queue.Empty:
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Error en stream_loop: {e}")
            await asyncio.sleep(0.5)