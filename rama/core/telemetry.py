import base64
import collections
import json
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

    El pipeline registra cada frame leído (hilo de cámara) y cada frame
    procesado (hilo principal). Los eventos se meten en self.cola para que
    el broadcaster del WebSocket /ws/debug los envíe, y además se guarda un
    histórico en memoria para que el dashboard recupere el estado al recargar.

    Agrupa los frames en "pasos de vehículo" (sesiones): una sesión empieza
    con la primera detección y termina cuando el detector deja de ver la
    matrícula durante más de detection.max_missed_frames frames seguidos
    (el mismo criterio que usa el validator para resetear el voting).

    De cada sesión se guardan en disco la captura completa del vehículo y
    el recorte de la matrícula (en el momento de la lectura, o el de mejor
    confianza del detector si no se llegó a leer), más una línea en
    sesiones.jsonl. Así el histórico sobrevive a reinicios.
    """

    def __init__(self, historico_frames=300, historico_sesiones=300):
        self._lock = threading.Lock()
        self.cola = queue.Queue(maxsize=500)
        self.frames = collections.deque(maxlen=historico_frames)
        self.sesiones = collections.deque(maxlen=historico_sesiones)
        self._ts_leidos = collections.deque(maxlen=600)
        self._t_lectura = collections.deque(maxlen=300)
        self._ts_procesados = collections.deque(maxlen=600)
        self._sesion = None
        self._siguiente_sesion = 1
        self._colas = {}

        debug_cfg = config.get('debug', {})
        self.dir_capturas = debug_cfg.get('captures_path', 'debug_captures')
        self.max_capturas = debug_cfg.get('max_captures', 1000)
        self._capturas = collections.deque()
        self._cola_disco = queue.Queue(maxsize=50)

        self.reset_contadores()
        self._cargar_historico()
        threading.Thread(target=self._escritor, daemon=True, name="hilo-capturas").start()

    def reset_contadores(self):
        """Pone a cero contadores e históricos en memoria (botón 'reset').
        Las capturas y sesiones.jsonl en disco se conservan."""
        with self._lock:
            self.inicio = time.time()
            self.contadores = collections.Counter()
            self.frames.clear()
            self.sesiones.clear()
            self._ts_leidos.clear()
            self._t_lectura.clear()
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

    # ── Persistencia de capturas ─────────────────────────────────────────────

    @property
    def _jsonl(self):
        return os.path.join(self.dir_capturas, 'sesiones.jsonl')

    def _cargar_historico(self):
        """Carga las últimas sesiones de sesiones.jsonl al arrancar."""
        try:
            os.makedirs(self.dir_capturas, exist_ok=True)
            self._capturas.extend(sorted(
                f for f in os.listdir(self.dir_capturas) if f.endswith('.jpg')
            ))
            if not os.path.exists(self._jsonl):
                return
            with open(self._jsonl, encoding='utf-8') as f:
                lineas = f.readlines()[-self.sesiones.maxlen:]
            for linea in lineas:
                try:
                    s = json.loads(linea)
                except ValueError:
                    continue
                self.sesiones.append(s)
                self._siguiente_sesion = max(self._siguiente_sesion, s.get('id', 0) + 1)
            logger.info(f"Telemetría: {len(self.sesiones)} sesiones cargadas de {self._jsonl}")
        except Exception as e:
            logger.error(f"No se pudo cargar el histórico de sesiones: {e}")

    def _escritor(self):
        """
        Hilo que guarda en disco las capturas de cada sesión cerrada.

        Codificar y escribir un JPG de resolución completa cuesta decenas
        de ms en la RPi, así que se hace fuera del hilo del pipeline.
        """
        while True:
            sesion, frame, crop = self._cola_disco.get()
            try:
                base = time.strftime('%Y%m%d-%H%M%S', time.localtime(sesion['start']))
                base = f"{base}_s{sesion['id']}"
                for sufijo, img, calidad in (('full', frame, 90), ('crop', crop, 95)):
                    if img is None or img.size == 0:
                        continue
                    nombre = f"{base}_{sufijo}.jpg"
                    cv2.imwrite(os.path.join(self.dir_capturas, nombre), img,
                                [cv2.IMWRITE_JPEG_QUALITY, calidad])
                    sesion[f'capture_{sufijo}'] = nombre
                    self._capturas.append(nombre)

                with open(self._jsonl, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(sesion, ensure_ascii=False) + '\n')

                self._purgar_capturas()
            except Exception as e:
                logger.error(f"Error guardando capturas de la sesión {sesion.get('id')}: {e}")

            with self._lock:
                self.sesiones.append(sesion)
            self._emitir({"type": "session", "data": sesion})

    def _purgar_capturas(self):
        """Borra las capturas más antiguas si se supera debug.max_captures."""
        while len(self._capturas) > self.max_capturas:
            nombre = self._capturas.popleft()
            try:
                os.remove(os.path.join(self.dir_capturas, nombre))
            except OSError:
                pass

    # ── Llamadas desde el pipeline ───────────────────────────────────────────

    def frame_leido(self, ok, read_ms):
        """Registra la lectura de un frame de la cámara (hilo de cámara)."""
        with self._lock:
            self.contadores['frames_leidos'] += 1
            if not ok:
                self.contadores['frames_nulos'] += 1
                return
            self._ts_leidos.append(time.time())
            self._t_lectura.append(read_ms)

    def frame_procesado(self, ev, crop=None, frame=None):
        """
        Registra un frame que ha pasado por detector/OCR/validator.

        Args:
            ev (dict): Datos del frame (tiempos, confianzas, decisión...).
            crop (numpy.ndarray|None): Recorte de la matrícula que vio el OCR.
            frame (numpy.ndarray|None): Frame completo (anotado con el bbox).
        """
        ahora = time.time()
        crop_b64 = _jpg_b64(crop) if crop is not None else None

        with self._lock:
            self.contadores['frames_procesados'] += 1
            self.contadores['frames_descartados'] += ev.get('dropped', 0)
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

            self._actualizar_sesion(ev, crop, frame, ahora)
            ev['session_id'] = self._sesion['id'] if self._sesion else None
            self.frames.append(ev)

        self._emitir({"type": "frame", "data": {**ev, "crop": crop_b64}})

    # ── Sesiones (pasos de vehículo) ─────────────────────────────────────────

    def _actualizar_sesion(self, ev, crop, frame, ahora):
        """Abre/actualiza/cierra la sesión actual."""
        if not ev.get('detected'):
            if self._sesion is None:
                return
            self._sesion["missed"] += 1
            if self._sesion["missed"] > config['detection'].get('max_missed_frames', 2):
                self._cerrar_sesion()
            return

        if self._sesion is None:
            self._sesion = {
                "id": self._siguiente_sesion,
                "start": ahora,
                "end": None,
                "duration_ms": None,
                "frames": 0,
                "missed": 0,
                "missed_total": 0,
                "ocr_reads": 0,
                "readings": {},
                "best_det_conf": 0.0,
                "result": None,
                "plate": None,
                "accepted_conf": None,
                "time_to_read_ms": None,
                "frames_to_read": None,
                "votes_at_accept": None,
                "last_decision": None,
                "fingerprint": None,
                "capture_bbox": None,
                "capture_wh": None,
                "_frame": None,
                "_crop": None,
            }
            self._siguiente_sesion += 1

        s = self._sesion
        s["missed_total"] += s["missed"]
        s["missed"] = 0
        s["frames"] += 1
        s["end"] = ahora

        if ev.get('ocr_text'):
            s["ocr_reads"] += 1
            r = s["readings"].setdefault(ev['ocr_text'], {"n": 0, "conf_sum": 0.0})
            r["n"] += 1
            r["conf_sum"] += ev.get('ocr_conf') or 0.0

        v = ev.get('validator') or {}
        if v.get('decision'):
            s["last_decision"] = v['decision']

        aceptada = v.get('decision') == 'aceptada' and s["plate"] is None
        # Captura: la del momento de la lectura; hasta entonces, la de mejor detección
        if aceptada or (s["plate"] is None and ev.get('det_conf', 0) >= s["best_det_conf"]):
            s["_frame"], s["_crop"] = frame, crop
            s["capture_bbox"] = ev.get('bbox')
            s["capture_wh"] = ev.get('frame_wh')
        s["best_det_conf"] = max(s["best_det_conf"], ev.get('det_conf', 0))

        if aceptada:
            s["plate"] = v.get('winner')
            s["accepted_conf"] = ev.get('ocr_conf')
            s["time_to_read_ms"] = round((ahora - s["start"]) * 1000)
            s["frames_to_read"] = s["frames"]
            s["votes_at_accept"] = v.get('votes')
            s["fingerprint"] = ev.get('fingerprint')

        # Actualización en vivo de la sesión abierta
        self._emitir({"type": "session_live", "data": self._sesion_publica(s)})

    def _cerrar_sesion(self):
        s = self._sesion
        self._sesion = None
        s["duration_ms"] = round((s["end"] - s["start"]) * 1000)
        if s["plate"]:
            s["result"] = "leida"
        elif s["last_decision"] == "cooldown":
            s["result"] = "cooldown"
        else:
            s["result"] = "perdida"
        self.contadores[f'sesiones_{s["result"]}'] += 1

        publica = self._sesion_publica(s)
        try:
            self._cola_disco.put_nowait((publica, s["_frame"], s["_crop"]))
        except queue.Full:
            logger.warning(f"Cola de capturas llena — sesión {s['id']} sin imágenes.")
            self.sesiones.append(publica)
            self._emitir({"type": "session", "data": publica})

    @staticmethod
    def _sesion_publica(s):
        lecturas = sorted(
            ({"text": t, "n": r["n"], "conf_avg": round(r["conf_sum"] / r["n"], 3)}
             for t, r in s["readings"].items()),
            key=lambda x: (x["n"], x["conf_avg"]), reverse=True,
        )
        out = {k: v for k, v in s.items() if k != "readings" and not k.startswith("_")}
        out["readings"] = lecturas[:5]
        out["n_distinct_readings"] = len(lecturas)
        out["best_det_conf"] = round(s["best_det_conf"], 3)
        return out

    def ruta_captura(self, nombre):
        """Ruta en disco de una captura, o None si el nombre no es válido."""
        nombre = os.path.basename(nombre)
        if not nombre.endswith('.jpg'):
            return None
        ruta = os.path.join(self.dir_capturas, nombre)
        return ruta if os.path.isfile(ruta) else None

    # ── Lecturas desde la API ────────────────────────────────────────────────

    def _fps(self, marcas, ventana=5.0):
        ahora = time.time()
        recientes = [t for t in marcas if ahora - t <= ventana]
        if len(recientes) < 2 or recientes[-1] <= recientes[0]:
            return 0.0
        return round((len(recientes) - 1) / (recientes[-1] - recientes[0]), 1)

    def stats(self):
        """Estadísticas agregadas para la cabecera y la tabla de tiempos."""
        with self._lock:
            frames = list(self.frames)
            sesiones = list(self.sesiones)
            contadores = dict(self.contadores)
            t_lectura = list(self._t_lectura)
            fps_camara = self._fps(self._ts_leidos)
            fps_proc = self._fps(self._ts_procesados)
            sesion_abierta = self._sesion_publica(self._sesion) if self._sesion else None

        tiempos = {"read": _resumen(t_lectura)}
        for etapa in ("wait", "detect", "ocr", "validate", "store", "total"):
            tiempos[etapa] = _resumen([f.get(f't_{etapa}_ms') for f in frames])
        tiempos["age"] = _resumen([f.get('age_ms') for f in frames])
        leidas = [s for s in sesiones if s.get("result") == "leida"]
        tiempos["time_to_read"] = _resumen([s.get("time_to_read_ms") for s in leidas])

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
                "max_missed_frames": config['detection'].get('max_missed_frames', 2),
                "cooldown_seconds": config['detection'].get('cooldown_seconds'),
                "country": config.get('location', {}).get('country'),
                "camera_fps": config['camera'].get('fps'),
                "camera_resolution": config['camera'].get('resolution'),
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
