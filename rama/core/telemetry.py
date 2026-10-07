import base64
import collections
import logging
import os
import queue
import threading
import time

import cv2

from config.config import config
from core import state

logger = logging.getLogger(__name__)


def _percentil(valores, p):
    """Percentil p (0-100) por el método del vecino más cercano."""
    if not valores:
        return None
    ordenados = sorted(valores)
    idx = min(len(ordenados) - 1, int(round(p / 100 * (len(ordenados) - 1))))
    return ordenados[idx]


def _resumen(valores):
    """Devuelve last/avg/p50/p95/max de una lista de tiempos en ms."""
    valores = [v for v in valores if v is not None]
    if not valores:
        return None
    return {
        "last": round(valores[-1], 1),
        "avg":  round(sum(valores) / len(valores), 1),
        "p50":  round(_percentil(valores, 50), 1),
        "p95":  round(_percentil(valores, 95), 1),
        "max":  round(max(valores), 1),
        "n":    len(valores),
    }


def _cpu_temp():
    """Temperatura de la CPU de la RPi en ºC, o None si no está disponible."""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


def _jpg_b64(img, quality=85):
    """Codifica una imagen BGR a JPG en base64 (para mandarla dentro del JSON)."""
    if img is None or img.size == 0:
        return None
    ok, buffer = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    return base64.b64encode(buffer.tobytes()).decode('ascii')


class Telemetry:
    """
    Recoge métricas de depuración del pipeline para el dashboard de pruebas.

    El pipeline (hilo principal) registra cada frame leído y cada frame
    procesado. Los eventos se meten en self.cola para que el broadcaster
    del WebSocket /ws/debug los envíe, y además se guarda un histórico
    en memoria para que el dashboard pueda recuperar el estado al recargar.

    Agrupa los frames en "pasos de vehículo" (sesiones): una sesión empieza
    con la primera detección tras un frame sin matrícula y termina cuando
    el detector deja de ver matrícula. Así se mide el tiempo que tarda el
    sistema en leer cada vehículo y cuántos vehículos se pierden sin lectura.
    """

    def __init__(self, historico_frames=300, historico_sesiones=200):
        self._lock = threading.Lock()
        self.cola = queue.Queue(maxsize=500)
        self.frames = collections.deque(maxlen=historico_frames)
        self.sesiones = collections.deque(maxlen=historico_sesiones)
        self._ts_leidos = collections.deque(maxlen=600)
        self._ts_procesados = collections.deque(maxlen=600)
        self._sesion = None
        self._siguiente_sesion = 1
        self._colas = {}
        self.frame_skip = None
        self.reset_contadores()

    def reset_contadores(self):
        """Pone a cero contadores e históricos (botón 'reset' del dashboard)."""
        with self._lock:
            self.inicio = time.time()
            self.contadores = collections.Counter()
            self.frames.clear()
            self.sesiones.clear()
            self._ts_leidos.clear()
            self._ts_procesados.clear()
            self._sesion = None

    def registrar_colas(self, **colas):
        """Registra colas (queue.Queue) cuyo tamaño se muestra en las stats."""
        self._colas.update(colas)

    def _emitir(self, evento):
        try:
            self.cola.put_nowait(evento)
        except queue.Full:
            # Nadie consume (sin dashboard abierto) — se descarta sin más
            pass

    # ── Llamadas desde el pipeline ───────────────────────────────────────────

    def frame_leido(self, ok, read_ms):
        """Registra la lectura de un frame de la cámara (procesado o no)."""
        with self._lock:
            self.contadores['frames_leidos'] += 1
            if not ok:
                self.contadores['frames_nulos'] += 1
            self._ts_leidos.append(time.time())

    def frame_procesado(self, ev, crop=None):
        """
        Registra un frame que ha pasado por detector/OCR/validator.

        Args:
            ev (dict): Datos del frame (tiempos, confianzas, decisión...).
            crop (numpy.ndarray|None): Recorte de la matrícula que vio el OCR.
        """
        ahora = time.time()
        crop_b64 = _jpg_b64(crop) if crop is not None else None

        with self._lock:
            self.contadores['frames_procesados'] += 1
            self._ts_procesados.append(ahora)

            if ev.get('detected'):
                self.contadores['detecciones'] += 1
                if ev.get('ocr_text'):
                    self.contadores['ocr_ok'] += 1
                else:
                    self.contadores['ocr_vacio'] += 1
            decision = (ev.get('validator') or {}).get('decision')
            if decision:
                self.contadores[f'decision_{decision}'] += 1

            sesion_cerrada = self._actualizar_sesion(ev, crop_b64, ahora)
            ev['session_id'] = self._sesion['id'] if self._sesion else None
            self.frames.append(ev)

        self._emitir({"type": "frame", "data": {**ev, "crop": crop_b64}})
        if sesion_cerrada:
            self._emitir({"type": "session", "data": sesion_cerrada})

    # ── Sesiones (pasos de vehículo) ─────────────────────────────────────────

    def _actualizar_sesion(self, ev, crop_b64, ahora):
        """Abre/actualiza/cierra la sesión actual. Devuelve la sesión cerrada o None."""
        if not ev.get('detected'):
            if self._sesion is None:
                return None
            return self._cerrar_sesion(ahora)

        if self._sesion is None:
            self._sesion = {
                "id": self._siguiente_sesion,
                "start": ahora,
                "end": None,
                "duration_ms": None,
                "frames": 0,
                "ocr_reads": 0,
                "readings": {},
                "best_det_conf": 0.0,
                "best_crop": None,
                "result": None,
                "plate": None,
                "accepted_conf": None,
                "time_to_read_ms": None,
                "frames_to_read": None,
                "votes_at_accept": None,
                "last_decision": None,
            }
            self._siguiente_sesion += 1

        s = self._sesion
        s["frames"] += 1
        if ev.get('det_conf', 0) >= s["best_det_conf"] and crop_b64:
            s["best_det_conf"] = ev['det_conf']
            s["best_crop"] = crop_b64

        if ev.get('ocr_text'):
            s["ocr_reads"] += 1
            r = s["readings"].setdefault(ev['ocr_text'], {"n": 0, "conf_sum": 0.0})
            r["n"] += 1
            r["conf_sum"] += ev.get('ocr_conf') or 0.0

        v = ev.get('validator') or {}
        if v.get('decision'):
            s["last_decision"] = v['decision']
        if v.get('decision') == 'aceptada' and s["plate"] is None:
            s["plate"] = v.get('winner')
            s["accepted_conf"] = ev.get('ocr_conf')
            s["time_to_read_ms"] = round((ahora - s["start"]) * 1000)
            s["frames_to_read"] = s["frames"]
            s["votes_at_accept"] = v.get('votes')

        # Actualización en vivo de la sesión abierta (sin el crop para no repetirlo)
        self._emitir({"type": "session_live", "data": self._sesion_publica(s, con_crop=False)})
        return None

    def _cerrar_sesion(self, ahora):
        s = self._sesion
        self._sesion = None
        s["end"] = ahora
        s["duration_ms"] = round((ahora - s["start"]) * 1000)
        if s["plate"]:
            s["result"] = "leida"
        elif s["last_decision"] == "cooldown":
            s["result"] = "cooldown"
        else:
            s["result"] = "perdida"
        self.contadores[f'sesiones_{s["result"]}'] += 1
        publica = self._sesion_publica(s)
        self.sesiones.append(publica)
        return publica

    @staticmethod
    def _sesion_publica(s, con_crop=True):
        lecturas = sorted(
            ({"text": t, "n": r["n"], "conf_avg": round(r["conf_sum"] / r["n"], 3)}
             for t, r in s["readings"].items()),
            key=lambda x: (x["n"], x["conf_avg"]), reverse=True,
        )
        out = {k: v for k, v in s.items() if k not in ("readings", "best_crop")}
        out["readings"] = lecturas[:5]
        out["n_distinct_readings"] = len(lecturas)
        out["best_det_conf"] = round(s["best_det_conf"], 3)
        if con_crop:
            out["best_crop"] = s["best_crop"]
        return out

    # ── Lecturas desde la API ────────────────────────────────────────────────

    def _fps(self, marcas, ventana=5.0):
        ahora = time.time()
        recientes = [t for t in marcas if ahora - t <= ventana]
        if len(recientes) < 2:
            return 0.0
        return round((len(recientes) - 1) / (recientes[-1] - recientes[0]), 1) if recientes[-1] > recientes[0] else 0.0

    def stats(self):
        """Estadísticas agregadas para la cabecera y la tabla de tiempos."""
        with self._lock:
            frames = list(self.frames)
            sesiones = list(self.sesiones)
            contadores = dict(self.contadores)
            fps_camara = self._fps(self._ts_leidos)
            fps_proc = self._fps(self._ts_procesados)
            sesion_abierta = self._sesion_publica(self._sesion, con_crop=False) if self._sesion else None

        tiempos = {
            etapa: _resumen([f.get(f't_{etapa}_ms') for f in frames])
            for etapa in ("read", "detect", "ocr", "validate", "store", "total")
        }
        leidas = [s for s in sesiones if s["result"] == "leida"]
        tiempos["time_to_read"] = _resumen([s["time_to_read_ms"] for s in leidas])

        try:
            carga = os.getloadavg()
        except OSError:
            carga = None

        return {
            "ts": time.time(),
            "uptime_s": round(time.time() - self.inicio),
            "fps_camera": fps_camara,
            "fps_processed": fps_proc,
            "timings": tiempos,
            "counters": contadores,
            "open_session": sesion_abierta,
            "system": {
                "camera_connected": state.camera_connected,
                "eqbroker_connected": state.eqbroker_connected,
                "cpu_temp": _cpu_temp(),
                "load_avg": carga,
                "queues": {nombre: c.qsize() for nombre, c in self._colas.items()},
            },
            "config": {
                "backend": config['detection'].get('backend'),
                "detection_threshold": config['detection'].get('threshold'),
                "ocr_threshold": config['ocr'].get('threshold'),
                "frames_for_voting": config['detection'].get('frames_for_voting'),
                "cooldown_seconds": config['detection'].get('cooldown_seconds'),
                "country": config.get('location', {}).get('country'),
                "frame_skip": self.frame_skip,
                "camera_id": config['eqbroker'].get('camera_id'),
            },
        }

    def snapshot(self):
        """Estado completo para un cliente que se acaba de conectar."""
        with self._lock:
            frames = list(self.frames)
            sesiones = list(self.sesiones)
        return {"stats": self.stats(), "frames": frames, "sessions": sesiones}


# Instancia única compartida por el pipeline y la API (mismo proceso)
telemetry = Telemetry()
