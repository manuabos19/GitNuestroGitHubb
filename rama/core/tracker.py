import logging

from config.config import config

logger = logging.getLogger(__name__)


def _iou(a, b):
    """Intersección sobre unión de dos cajas (x1, y1, x2, y2)."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _centro(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


class PlateTracker:
    """
    Seguimiento de UN vehículo (su matrícula) entre frames.

    Cuando aparece una matrícula se "engancha" a ella y en los frames
    siguientes solo acepta la detección que sea continuación de esa
    (posición y tamaño coherentes con el movimiento). Cualquier otra
    matrícula del frame (el coche de detrás, uno que pasa por la calle...)
    se ignora hasta que el vehículo seguido desaparece durante más de
    detection.max_missed_frames frames seguidos. Solo entonces se puede
    enganchar el siguiente.

    Al empezar un seguimiento elige la matrícula más grande del frame
    (la del vehículo más cercano a la cámara), no la de más confianza.

    Excepción: si otra matrícula es claramente más grande que la seguida
    (switch_ratio veces su ancho) durante switch_frames frames seguidos,
    el seguimiento pasa a ella. Así un coche parado o que espera detrás
    no puede "secuestrar" el seguimiento mientras el de delante entra.

    Parámetros en settings.yaml:

        tracking:
          enabled: true
          max_jump: 2.0       # desplazamiento máximo entre frames, en anchos de matrícula
          switch_ratio: 1.3   # 0 desactiva el cambio a un vehículo más cercano
          switch_frames: 3
    """

    def __init__(self):
        self.track = None
        self._siguiente_id = 1
        self._rival = 0  # frames seguidos con una matrícula más cercana que la seguida

    def reset(self):
        self.track = None
        self._rival = 0

    def _iniciar(self, det):
        self.track = {"id": self._siguiente_id, "bbox": det["bbox"], "vel": (0.0, 0.0),
                      "hits": 1, "missed": 0}
        self._siguiente_id += 1
        self._rival = 0
        logger.debug(f"Seguimiento {self.track['id']} iniciado en {det['bbox']}")

    @staticmethod
    def _ancho(det):
        return det["bbox"][2] - det["bbox"][0]

    def _prediccion(self):
        """Caja donde se espera la matrícula en este frame (posición + velocidad)."""
        t = self.track
        vx, vy = t["vel"]
        x1, y1, x2, y2 = t["bbox"]
        pasos = t["missed"] + 1
        return (x1 + vx * pasos, y1 + vy * pasos, x2 + vx * pasos, y2 + vy * pasos)

    def _encaja(self, det, prediccion, max_jump):
        """Puntuación de lo bien que encaja una detección con el seguimiento (None = no encaja)."""
        ancho_t = prediccion[2] - prediccion[0]
        ancho_d = det["bbox"][2] - det["bbox"][0]
        # Entre frames seguidos la matrícula apenas cambia de tamaño; una de
        # otro vehículo (más cerca o más lejos de la cámara) sí. Este filtro
        # evita que el seguimiento salte al coche de detrás cuando el seguido se va.
        if ancho_t <= 0 or not (0.7 <= ancho_d / ancho_t <= 1.43):
            return None

        iou = _iou(prediccion, det["bbox"])
        (pcx, pcy), (dcx, dcy) = _centro(prediccion), _centro(det["bbox"])
        distancia = ((pcx - dcx) ** 2 + (pcy - dcy) ** 2) ** 0.5 / ancho_t
        if iou < 0.1 and distancia > max_jump:
            return None
        return iou - distancia * 0.1

    def update(self, detecciones):
        """
        Actualiza el seguimiento con las detecciones del frame.

        Args:
            detecciones (list[dict]): [{"bbox": (x1, y1, x2, y2), "conf": float}, ...]

        Returns:
            tuple: (detección elegida o None, info) donde info es
                   {"id", "new", "ended", "switched", "ignored", "hits", "missed"}.
                   "ended" es el id del seguimiento que acaba de terminar; si
                   "switched" es True, en el mismo frame empieza otro ("id").
        """
        max_missed = config['detection'].get('max_missed_frames', 2)
        tracking_cfg = config.get('tracking') or {}
        max_jump = tracking_cfg.get('max_jump', 2.0)
        switch_ratio = tracking_cfg.get('switch_ratio', 1.3)
        switch_frames = tracking_cfg.get('switch_frames', 3)
        info = {"id": None, "new": False, "ended": None, "switched": False,
                "ignored": 0, "hits": 0, "missed": 0}

        if self.track is None:
            if not detecciones:
                return None, info
            det = max(detecciones, key=lambda d: (d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1]))
            self._iniciar(det)
            info.update(id=self.track["id"], new=True, hits=1, ignored=len(detecciones) - 1)
            return det, info

        t = self.track
        prediccion = self._prediccion()
        candidatas = [(self._encaja(d, prediccion, max_jump), d) for d in detecciones]
        candidatas = [(p, d) for p, d in candidatas if p is not None]

        det = max(candidatas, key=lambda x: x[0])[1] if candidatas else None

        # ¿Hay otra matrícula claramente más cercana que la seguida?
        otras = [d for d in detecciones if d is not det]
        cercana = max(otras, key=self._ancho) if otras else None
        ancho_seguido = self._ancho(det) if det else t["bbox"][2] - t["bbox"][0]
        if switch_ratio and cercana and self._ancho(cercana) >= switch_ratio * ancho_seguido:
            self._rival += 1
        else:
            self._rival = 0
        if switch_ratio and self._rival >= switch_frames:
            logger.debug(f"Seguimiento {t['id']} cambia a un vehículo más cercano.")
            info["ended"] = t["id"]
            self._iniciar(cercana)
            info.update(id=self.track["id"], new=True, switched=True, hits=1,
                        ignored=len(detecciones) - 1)
            return cercana, info

        if det is not None:
            (ocx, ocy), (ncx, ncy) = _centro(t["bbox"]), _centro(det["bbox"])
            pasos = t["missed"] + 1
            # Velocidad suavizada para predecir dónde estará en el siguiente frame
            vx, vy = (ncx - ocx) / pasos, (ncy - ocy) / pasos
            t["vel"] = (0.5 * t["vel"][0] + 0.5 * vx, 0.5 * t["vel"][1] + 0.5 * vy)
            t["bbox"] = det["bbox"]
            t["hits"] += 1
            t["missed"] = 0
            info.update(id=t["id"], hits=t["hits"], ignored=len(detecciones) - 1)
            return det, info

        t["missed"] += 1
        info.update(id=t["id"], hits=t["hits"], missed=t["missed"], ignored=len(detecciones))
        if t["missed"] > max_missed:
            logger.debug(f"Seguimiento {t['id']} terminado ({t['hits']} frames).")
            info["ended"] = t["id"]
            self.track = None
        return None, info
