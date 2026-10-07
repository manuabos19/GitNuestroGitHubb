import asyncio
import json
import logging
import os
import queue
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from core.telemetry import telemetry

router = APIRouter()
logger = logging.getLogger(__name__)

DEBUG_HTML = os.path.join(os.path.dirname(__file__), '..', 'static', 'debug.html')


class DebugManager:
    """
    Gestiona las conexiones WebSocket del dashboard de depuración.

    A diferencia del stream de cámara, el broadcaster de este canal se
    lanza en el propio event loop de uvicorn (ver start_broadcaster),
    así los envíos se hacen desde el mismo loop que aceptó la conexión.
    """

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)
        logger.info(f"Cliente conectado a /ws/debug. Total: {len(self.connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)
            logger.info(f"Cliente desconectado de /ws/debug. Total: {len(self.connections)}")

    async def broadcast(self, texto: str):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(texto)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)


debug_manager = DebugManager()


@router.get('/debug')
async def get_debug_page():
    """Sirve el dashboard de depuración (http://IP_RPI:8000/debug)."""
    return FileResponse(DEBUG_HTML)


@router.get('/api/debug/snapshot')
async def get_debug_snapshot():
    """
    Devuelve stats, últimos frames procesados y sesiones cerradas.

    Returns:
        dict: {stats, frames, sessions}
    """
    return telemetry.snapshot()


@router.post('/api/debug/reset')
async def reset_debug():
    """Pone a cero contadores, históricos y sesiones de la telemetría."""
    telemetry.reset_contadores()
    logger.info("Telemetría reseteada desde el dashboard.")
    return {"status": "ok"}


@router.websocket('/ws/debug')
async def websocket_debug(websocket: WebSocket):
    """
    WebSocket de telemetría. Al conectar envía un snapshot completo y
    después los eventos en tiempo real:
      - {"type": "frame",        "data": {...}}  cada frame procesado
      - {"type": "session_live", "data": {...}}  sesión de vehículo en curso
      - {"type": "session",      "data": {...}}  sesión de vehículo cerrada
      - {"type": "stats",        "data": {...}}  estadísticas cada segundo
    """
    await debug_manager.connect(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "snapshot", "data": telemetry.snapshot()}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("WS /debug desconectado por el cliente.")
    except Exception as e:
        logger.error(f"Error en WS /debug: {e}")
    finally:
        debug_manager.disconnect(websocket)


async def debug_loop():
    """
    Corrutina que vacía la cola de telemetría y la reenvía a los clientes.

    Agrupa los eventos pendientes en cada vuelta para no saturar el
    WebSocket y manda las estadísticas agregadas una vez por segundo.
    """
    ultimo_stats = 0.0
    loop = asyncio.get_running_loop()

    while True:
        try:
            eventos = []
            while len(eventos) < 50:
                try:
                    eventos.append(telemetry.cola.get_nowait())
                except queue.Empty:
                    break

            if not debug_manager.connections:
                await asyncio.sleep(0.2)
                continue

            for evento in eventos:
                await debug_manager.broadcast(json.dumps(evento))

            if loop.time() - ultimo_stats >= 1.0:
                ultimo_stats = loop.time()
                await debug_manager.broadcast(json.dumps({"type": "stats", "data": telemetry.stats()}))

            if not eventos:
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Error en debug_loop: {e}")
            await asyncio.sleep(0.5)


def start_broadcaster():
    """Lanza debug_loop en el event loop actual (el de uvicorn)."""
    asyncio.get_running_loop().create_task(debug_loop())
    logger.info("Broadcaster de /ws/debug arrancado.")
