from config.config import config
from ultralytics import YOLO
import logging

logger = logging.getLogger(__name__)


class DetectorYOLO:
    """
    Gestiona la detección de matrículas en frames de vídeo usando YOLO.

    Carga el modelo especificado en settings.yaml y devuelve el recorte
    de la matrícula con mayor confianza dentro del frame.
    """

    def __init__(self):
        """
        Carga el modelo YOLO desde la ruta definida en la configuración.
        """
        model_path = config['detection']['model']
        self.model = YOLO(model_path)
        logger.info(f"Modelo YOLO cargado desde {model_path}")

    def detect(self, frame):
        """
        Detecta la matrícula con mayor confianza en el frame recibido.

        Filtra las detecciones por el threshold definido en settings.yaml
        y devuelve únicamente la detección más fiable.

        Args:
            frame (numpy.ndarray): Frame completo capturado por la cámara en formato BGR.

        Returns:
            tuple: (recorte, confianza, bbox) donde recorte es numpy.ndarray con la
                   zona de la matrícula, confianza es un float entre 0 y 1 y bbox
                   es (x1, y1, x2, y2) en coordenadas del frame.
                   Devuelve (None, 0.0, (0, 0, 0, 0)) si no hay detecciones válidas,
                   igual que DetectorCoral, que es lo que espera el pipeline.
        """
        result_license_plates = self.model(frame, verbose=False)

        best_confidence = 0.0
        x1, y1, x2, y2 = None, None, None, None

        for result in result_license_plates:
            for box in result.boxes:
                confidence = float(box.conf[0])
                if confidence > best_confidence and confidence > config['detection']['threshold']:
                    best_confidence = confidence
                    x1, y1, x2, y2 = map(int, box.xyxy[0])

        if x1 is None:
            logger.debug("No se detectó ninguna matrícula válida en el frame.")
            return None, 0.0, (0, 0, 0, 0)

        logger.debug(f"Matrícula detectada con confianza {best_confidence:.2f} en [{x1},{y1},{x2},{y2}]")
        return frame[y1:y2, x1:x2], best_confidence, (x1, y1, x2, y2)