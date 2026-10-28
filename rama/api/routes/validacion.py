import json
import logging
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from config.config import config
from core.telemetry import telemetry, nombre_sitio

router = APIRouter()
logger = logging.getLogger(__name__)

VALIDACION_HTML = os.path.join(os.path.dirname(__file__), '..', 'static', 'validacion.html')


@router.get('/validacion')
async def get_validacion_page():
    """
    Sirve la página de validación (http://IP_RPI:8000/validacion).

    Compara las lecturas de RAMA entre dos fechas con las de otro sistema
    de reconocimiento. La carpeta de imágenes del otro sistema la lee el
    navegador del equipo donde está (p. ej. C:\\matriculas), no la RPi.
    """
    return FileResponse(VALIDACION_HTML)


def _captura_si_existe(nombre):
    """Devuelve el nombre de la captura si sigue en disco (las antiguas se purgan)."""
    if nombre and telemetry.ruta_captura(nombre):
        return nombre
    return None


@router.get('/api/validacion/lecturas')
def get_lecturas(desde: float = Query(..., description="Inicio, epoch en segundos"),
                 hasta: float = Query(..., description="Fin, epoch en segundos")):
    """
    Lecturas aceptadas por RAMA entre dos instantes, leídas de sesiones.jsonl
    (el histórico completo, no solo las sesiones en memoria).

    El instante de cada lectura es el de la aceptación de la matrícula
    (inicio de la sesión + tiempo hasta lectura). Es una función síncrona a
    propósito: FastAPI la ejecuta en un hilo aparte y leer un fichero grande
    no bloquea el event loop de los WebSockets.

    Args:
        desde (float): Inicio del rango (epoch, segundos).
        hasta (float): Fin del rango (epoch, segundos).

    Returns:
        dict: {site_name, camera_id, desde, hasta, lecturas: [...], perdidas}

    Raises:
        HTTPException 422: Si el rango no es válido.
    """
    if hasta <= desde:
        raise HTTPException(status_code=422, detail="La fecha de fin debe ser posterior a la de inicio.")

    lecturas = []
    perdidas = 0
    ruta = telemetry.ruta_sesiones
    if os.path.exists(ruta):
        with open(ruta, encoding='utf-8') as f:
            for linea in f:
                try:
                    s = json.loads(linea)
                except ValueError:
                    continue
                inicio = s.get('start') or 0
                ts = inicio + (s.get('time_to_read_ms') or 0) / 1000
                if not (desde <= ts <= hasta):
                    continue
                if not s.get('plate'):
                    if s.get('result') == 'perdida' and s.get('frames', 0) >= 2:
                        perdidas += 1
                    continue
                lecturas.append({
                    "id": s.get('id'),
                    "ts": round(ts, 3),
                    "start": inicio,
                    "plate": s['plate'],
                    "time_to_read_ms": s.get('time_to_read_ms'),
                    "accepted_conf": s.get('accepted_conf'),
                    "accepted_on_exit": s.get('accepted_on_exit', False),
                    "readings": s.get('readings', []),
                    "capture_full": _captura_si_existe(s.get('capture_full')),
                    "capture_crop": _captura_si_existe(s.get('capture_crop')),
                    "capture_bbox": s.get('capture_bbox'),
                })

    lecturas.sort(key=lambda x: x['ts'])
    logger.info(f"Validación: {len(lecturas)} lecturas entre {desde} y {hasta}.")
    return {
        "site_name": nombre_sitio(),
        "camera_id": config['eqbroker'].get('camera_id'),
        "desde": desde,
        "hasta": hasta,
        "lecturas": lecturas,
        "perdidas": perdidas,
    }
