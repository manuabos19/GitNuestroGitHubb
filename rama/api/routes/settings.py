from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from config.config import config
import logging
import yaml

router = APIRouter()
logger = logging.getLogger(__name__)

SETTINGS_PATH = "config/settings.yaml"


@router.get('/settings')
async def get_settings():
    """
    Devuelve la configuración actual del sistema RAMA.

    Returns:
        dict: Configuración completa leída desde settings.yaml.
    """
    logger.debug("GET /settings — devolviendo configuración actual.")
    return {
        "camera_ip":            config['camera']['ip'],
        "camera_user":          config['camera']['user'],
        "camera_password":      config['camera']['password'],
        "camera_port":          config['camera']['port'],
        "camera_fps":           config['camera']['fps'],
        "camera_stream_path":   config['camera']['stream_path'],
        "camera_width":         config['camera']['resolution']['width'],
        "camera_height":        config['camera']['resolution']['height'],
        "detection_threshold":  config['detection']['threshold'],
        "detection_backend":    config['detection']['backend'],
        "frames_for_voting":    config['detection']['frames_for_voting'],
        "cooldown_seconds":     config['detection']['cooldown_seconds'],
        "ocr_threshold":        config['ocr']['threshold'],
        "location_country":     config['location']['country'],
        "eqbroker_ip":          config['eqbroker']['eqbroker_ip'],
        "eqbroker_port":        config['eqbroker']['eqbroker_port'],
        "camera_id":            config['eqbroker']['camera_id'],
        "camera_name":          config['eqbroker']['camera_name'],
    }


class SettingsBody(BaseModel):
    camera_ip:           Optional[str]   = None
    camera_user:         Optional[str]   = None
    camera_password:     Optional[str]   = None
    camera_port:         Optional[int]   = None
    camera_fps:          Optional[int]   = None
    camera_stream_path:  Optional[str]   = None
    camera_width:        Optional[int]   = None
    camera_height:       Optional[int]   = None
    detection_threshold: Optional[float] = None
    detection_backend:   Optional[str]   = None
    frames_for_voting:   Optional[int]   = None
    cooldown_seconds:    Optional[int]   = None
    ocr_threshold:       Optional[float] = None
    location_country:    Optional[str]   = None
    eqbroker_ip:         Optional[str]   = None
    eqbroker_port:       Optional[int]   = None
    camera_id:           Optional[str]   = None
    camera_name:         Optional[str]   = None


def _save_settings():
    """
    Persiste la configuración actual en disco escribiendo settings.yaml.

    Lee el yaml actual, actualiza los valores desde config y lo escribe
    de vuelta para mantener la estructura original del fichero.
    """
    with open(SETTINGS_PATH, 'r') as f:
        settings = yaml.safe_load(f)

    settings['camera']['ip']                    = config['camera']['ip']
    settings['camera']['user']                  = config['camera']['user']
    settings['camera']['password']              = config['camera']['password']
    settings['camera']['port']                  = config['camera']['port']
    settings['camera']['fps']                   = config['camera']['fps']
    settings['camera']['stream_path']           = config['camera']['stream_path']
    settings['camera']['resolution']['width']   = config['camera']['resolution']['width']
    settings['camera']['resolution']['height']  = config['camera']['resolution']['height']
    settings['detection']['threshold']          = config['detection']['threshold']
    settings['detection']['backend']            = config['detection']['backend']
    settings['detection']['frames_for_voting']  = config['detection']['frames_for_voting']
    settings['detection']['cooldown_seconds']   = config['detection']['cooldown_seconds']
    settings['ocr']['threshold']                = config['ocr']['threshold']
    settings['location']['country']             = config['location']['country']
    settings['eqbroker']['eqbroker_ip']         = config['eqbroker']['eqbroker_ip']
    settings['eqbroker']['eqbroker_port']       = config['eqbroker']['eqbroker_port']
    settings['eqbroker']['camera_id']           = config['eqbroker']['camera_id']
    settings['eqbroker']['camera_name']         = config['eqbroker']['camera_name']

    with open(SETTINGS_PATH, 'w') as f:
        yaml.dump(settings, f, default_flow_style=False, allow_unicode=True)

    logger.debug("Settings guardados en disco.")


@router.post('/settings')
async def update_settings(body: SettingsBody):
    """
    Actualiza la configuración del sistema RAMA en memoria y en disco.

    Solo actualiza los campos que vienen en el body (no son None).
    Los cambios se persisten en settings.yaml inmediatamente.

    Args:
        body (SettingsBody): Campos a actualizar. Los campos no enviados
                             mantienen su valor actual.

    Returns:
        dict: Confirmación de la actualización.
    """
    logger.debug(f"POST /settings — actualizando configuración: {body}")

    if body.camera_ip           is not None: config['camera']['ip']                   = body.camera_ip
    if body.camera_user         is not None: config['camera']['user']                 = body.camera_user
    if body.camera_password     is not None: config['camera']['password']             = body.camera_password
    if body.camera_port         is not None: config['camera']['port']                 = body.camera_port
    if body.camera_fps          is not None: config['camera']['fps']                  = body.camera_fps
    if body.camera_stream_path  is not None: config['camera']['stream_path']          = body.camera_stream_path
    if body.camera_width        is not None: config['camera']['resolution']['width']  = body.camera_width
    if body.camera_height       is not None: config['camera']['resolution']['height'] = body.camera_height
    if body.detection_threshold is not None: config['detection']['threshold']         = body.detection_threshold
    if body.detection_backend   is not None: config['detection']['backend']           = body.detection_backend
    if body.frames_for_voting   is not None: config['detection']['frames_for_voting'] = body.frames_for_voting
    if body.cooldown_seconds    is not None: config['detection']['cooldown_seconds']  = body.cooldown_seconds
    if body.ocr_threshold       is not None: config['ocr']['threshold']               = body.ocr_threshold
    if body.location_country    is not None: config['location']['country']            = body.location_country
    if body.eqbroker_ip         is not None: config['eqbroker']['eqbroker_ip']        = body.eqbroker_ip
    if body.eqbroker_port       is not None: config['eqbroker']['eqbroker_port']      = body.eqbroker_port
    if body.camera_id           is not None: config['eqbroker']['camera_id']          = body.camera_id
    if body.camera_name         is not None: config['eqbroker']['camera_name']        = body.camera_name

    _save_settings()

    logger.info("Configuración actualizada correctamente.")
    return {"status": "ok", "message": "Configuración actualizada"}