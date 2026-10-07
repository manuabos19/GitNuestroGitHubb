import logging

import cv2
import numpy as np

from config.config import config

logger = logging.getLogger(__name__)


class Roi:
    """
    Zona de detección (ROI) definida como un polígono en coordenadas
    normalizadas (0-1) del frame, en settings.yaml:

        detection:
          roi:
            enabled: true
            polygon: [[0.30, 0.45], [0.85, 0.45], [0.95, 1.0], [0.20, 1.0]]

    Si está activa, el detector solo recibe el rectángulo que contiene el
    polígono, con lo de fuera del polígono en negro. Esto tiene dos efectos:
      - Las matrículas fuera de la zona (coches de detrás, la calle...)
        no se detectan.
      - Al ser una imagen más pequeña, la matrícula ocupa más píxeles en
        la entrada de 640×640 del modelo, así que se detecta mejor y de
        más lejos.

    La config se lee en cada frame, así que los cambios hechos desde el
    dashboard se aplican al momento.
    """

    def __init__(self):
        self._clave = None
        self._rect = None     # (x, y, w, h) del rectángulo que contiene el polígono
        self._mascara = None  # máscara del polígono dentro de ese rectángulo
        self._puntos = None   # polígono en píxeles del frame

    @staticmethod
    def _config():
        return config['detection'].get('roi') or {}

    def activa(self):
        """True si la ROI está activada y tiene un polígono válido."""
        c = self._config()
        return bool(c.get('enabled')) and len(c.get('polygon') or []) >= 3

    def _preparar_geometria(self, w, h):
        poligono = self._config()['polygon']
        clave = (w, h, tuple(tuple(p) for p in poligono))
        if clave == self._clave:
            return

        puntos = np.array([[min(max(x, 0.0), 1.0) * (w - 1), min(max(y, 0.0), 1.0) * (h - 1)]
                           for x, y in poligono], dtype=np.int32)
        x, y, rw, rh = cv2.boundingRect(puntos)
        mascara = np.zeros((rh, rw), dtype=np.uint8)
        cv2.fillPoly(mascara, [puntos - [x, y]], 255)

        self._clave = clave
        self._rect = (x, y, rw, rh)
        self._mascara = mascara
        self._puntos = puntos
        logger.info(f"ROI preparada: rect={self._rect} en frame {w}x{h}")

    def recortar(self, frame):
        """
        Devuelve la imagen que debe ver el detector.

        Args:
            frame (numpy.ndarray): Frame completo BGR.

        Returns:
            tuple: (imagen, (offset_x, offset_y)). Las coordenadas que
                   devuelva el detector sobre `imagen` hay que sumarles
                   el offset para llevarlas al frame completo.
        """
        h, w = frame.shape[:2]
        self._preparar_geometria(w, h)
        x, y, rw, rh = self._rect
        zona = frame[y:y + rh, x:x + rw]
        return cv2.bitwise_and(zona, zona, mask=self._mascara), (x, y)

    def contiene(self, bbox):
        """True si el centro del bbox (en píxeles del frame) está dentro del polígono."""
        if self._puntos is None:
            return True
        x1, y1, x2, y2 = bbox
        centro = (float((x1 + x2) / 2), float((y1 + y2) / 2))
        return cv2.pointPolygonTest(self._puntos, centro, False) >= 0

    def dibujar(self, img):
        """Dibuja el polígono sobre una imagen de cualquier tamaño (in situ)."""
        if not self.activa():
            return
        h, w = img.shape[:2]
        puntos = np.array([[x * (w - 1), y * (h - 1)] for x, y in self._config()['polygon']], dtype=np.int32)
        cv2.polylines(img, [puntos], True, (255, 200, 0), max(1, w // 400), cv2.LINE_AA)
