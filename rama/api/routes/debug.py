import asyncio
import json
import logging
import os
import queue
from typing import List, Optional

import cv2
import yaml
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from config.config import config
from core.telemetry import telemetry

router = APIRouter()
logger = logging.getLogger(__name__)

DEBUG_HTML = os.path.join(os.path.dirname(__file__), '..', 'static', 'debug.html')
SETTINGS_PATH = "config/settings.yaml"


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


@router.get('/api/debug/captures/{filename}')
async def get_debug_capture(filename: str):
    """
    Sirve una captura guardada de un paso de vehículo
    (<fecha>_s<id>_full.jpg o <fecha>_s<id>_crop.jpg).

    Raises:
        HTTPException 404: Si la captura no existe (o ya se purgó).
    """
    ruta = telemetry.ruta_captura(filename)
    if ruta is None:
        raise HTTPException(status_code=404, detail="Captura no encontrada.")
    return FileResponse(ruta, media_type="image/jpeg")


@router.get('/api/debug/frame.jpg')
async def get_debug_frame():
    """
    Último frame de la cámara a resolución completa (para dibujar la ROI).

    Raises:
        HTTPException 503: Si todavía no hay frames.
    """
    frame = telemetry.grabber.ultimo() if telemetry.grabber else None
    if frame is None:
        raise HTTPException(status_code=503, detail="Todavía no hay frames de la cámara.")
    ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise HTTPException(status_code=500, detail="No se pudo codificar el frame.")
    return Response(buffer.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


class DeteccionBody(BaseModel):
    roi_enabled:         Optional[bool]              = None
    roi_polygon:         Optional[List[List[float]]] = None
    tracking_enabled:    Optional[bool]              = None
    decide_on_exit:      Optional[bool]              = None
    skip_ocr_after_read: Optional[bool]              = None
    min_frames_on_exit:  Optional[int]               = None
    max_jump:            Optional[float]             = None
    switch_ratio:        Optional[float]             = None
    max_missed_frames:   Optional[int]               = None


def _guardar_deteccion():
    """
    Persiste detection.roi, detection.max_missed_frames y tracking en
    settings.yaml, igual que settings.py: lee el yaml, cambia solo esas
    claves y lo vuelve a escribir.

    Returns:
        bool: True si se guardó, False si no se pudo (queda solo en memoria).
    """
    try:
        with open(SETTINGS_PATH, 'r') as f:
            settings = yaml.safe_load(f)
        settings.setdefault('detection', {})
        settings['detection']['roi'] = config['detection'].get('roi')
        settings['detection']['max_missed_frames'] = config['detection'].get('max_missed_frames', 2)
        settings['tracking'] = config.get('tracking')
        with open(SETTINGS_PATH, 'w') as f:
            yaml.dump(settings, f, default_flow_style=False, allow_unicode=True)
        return True
    except Exception as e:
        logger.error(f"No se pudo guardar la configuración de detección en {SETTINGS_PATH}: {e}")
        return False


@router.post('/api/debug/detection-config')
async def update_detection_config(body: DeteccionBody):
    """
    Activa/desactiva la ROI y el seguimiento y ajusta sus parámetros.

    Se aplica en caliente (el pipeline lee la config en cada frame) y se
    guarda en settings.yaml. Solo cambia los campos que vienen en el body.

    Raises:
        HTTPException 422: Si el polígono no es válido.
    """
    roi = dict(config['detection'].get('roi') or {"enabled": False, "polygon": []})
    tracking = dict(config.get('tracking') or {"enabled": False})

    if body.roi_polygon is not None:
        poligono = body.roi_polygon
        if poligono and (len(poligono) < 3 or any(
                len(p) != 2 or not all(0.0 <= v <= 1.0 for v in p) for p in poligono)):
            raise HTTPException(status_code=422,
                                detail="El polígono necesita al menos 3 puntos [x, y] entre 0 y 1.")
        roi['polygon'] = [[round(x, 4), round(y, 4)] for x, y in poligono]
    if body.roi_enabled is not None:
        roi['enabled'] = body.roi_enabled
    if roi.get('enabled') and len(roi.get('polygon') or []) < 3:
        raise HTTPException(status_code=422, detail="Dibuja la zona antes de activarla.")

    if body.tracking_enabled    is not None: tracking['enabled']             = body.tracking_enabled
    if body.decide_on_exit      is not None: tracking['decide_on_exit']      = body.decide_on_exit
    if body.skip_ocr_after_read is not None: tracking['skip_ocr_after_read'] = body.skip_ocr_after_read
    if body.min_frames_on_exit  is not None: tracking['min_frames_on_exit']  = max(1, body.min_frames_on_exit)
    if body.max_jump            is not None: tracking['max_jump']            = max(0.1, body.max_jump)
    if body.switch_ratio        is not None: tracking['switch_ratio']        = max(0.0, body.switch_ratio)
    if body.max_missed_frames   is not None:
        config['detection']['max_missed_frames'] = max(0, body.max_missed_frames)

    # Se sustituyen los dicts enteros: el pipeline nunca ve uno a medio cambiar
    config['detection']['roi'] = roi
    config['tracking'] = tracking

    guardado = _guardar_deteccion()
    logger.info(f"Configuración de detección actualizada: roi={roi} tracking={tracking}")
    return {"status": "ok", "saved": guardado, "roi": roi, "tracking": tracking,
            "max_missed_frames": config['detection'].get('max_missed_frames', 2)}


@router.post('/api/debug/reset')
async def reset_debug():
    """Pone a cero contadores e históricos en memoria (las capturas en disco se conservan)."""
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
