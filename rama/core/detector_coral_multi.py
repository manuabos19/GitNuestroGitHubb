import logging

import cv2
import numpy as np

from core.detector_coral import DetectorCoral

logger = logging.getLogger(__name__)


class DetectorCoralMulti(DetectorCoral):
    """
    DetectorCoral que además puede devolver TODAS las matrículas del frame.

    DetectorCoral.detect() solo devuelve la de mayor confianza. Para la zona
    de detección (ROI) y el seguimiento del vehículo hace falta ver todas,
    porque la de mayor confianza puede ser la del coche de detrás.

    Reutiliza el modelo, el intérprete y el preprocesado (letterbox) de
    DetectorCoral; solo cambia el postprocesado, que añade NMS para quitar
    cajas duplicadas de la misma matrícula.
    """

    def __init__(self, nms_iou=0.5):
        super().__init__()
        self.nms_iou = nms_iou

    def detect_all(self, frame):
        """
        Detecta todas las matrículas del frame por encima del threshold.

        Args:
            frame (numpy.ndarray): Imagen BGR (frame completo o zona ROI).

        Returns:
            list[dict]: [{"bbox": (x1, y1, x2, y2), "conf": float}, ...]
                        en coordenadas de `frame`, ordenadas por confianza.
        """
        original_h, original_w = frame.shape[:2]
        frame_batch, ratio, padding_x, padding_y = self._preprocess(frame)

        self.interpreter.set_tensor(self.input_details[0]['index'], frame_batch)
        self.interpreter.invoke()
        output = self.interpreter.get_tensor(self.output_details[0]['index'])

        # Desescalado int8 → float y (1, 5, 8400) → (8400, 5): cx, cy, w, h, conf
        salida = (output.astype(np.float32) - self.zero_point) * self.scale
        salida = np.squeeze(salida, 0).T
        salida = salida[salida[:, 4] > self.threshold]
        if len(salida) == 0:
            return []

        # Coordenadas normalizadas del modelo → píxeles del frame original
        cx, cy, w, h = (salida[:, i] * 640 for i in range(4))
        x1 = np.clip((cx - w / 2 - padding_x) / ratio, 0, original_w)
        y1 = np.clip((cy - h / 2 - padding_y) / ratio, 0, original_h)
        x2 = np.clip((cx + w / 2 - padding_x) / ratio, 0, original_w)
        y2 = np.clip((cy + h / 2 - padding_y) / ratio, 0, original_h)
        confs = salida[:, 4]

        cajas = [[float(a), float(b), float(c - a), float(d - b)] for a, b, c, d in zip(x1, y1, x2, y2)]
        indices = cv2.dnn.NMSBoxes(cajas, confs.tolist(), float(self.threshold), self.nms_iou)

        detecciones = []
        for i in np.array(indices).flatten():
            caja = (int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i]))
            if caja[2] - caja[0] < 2 or caja[3] - caja[1] < 2:
                continue
            detecciones.append({"bbox": caja, "conf": float(confs[i])})

        detecciones.sort(key=lambda d: d["conf"], reverse=True)
        return detecciones
