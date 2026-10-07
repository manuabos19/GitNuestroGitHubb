import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


class Fingerprint:
    """
    Módulo de huella digital del vehículo.

    Analiza el frame en el momento de aceptar una matrícula para
    extraer características visuales del vehículo: color de carrocería.
    Solo se ejecuta una vez por vehículo cuando el validator acepta,
    por lo que no impacta en el rendimiento del pipeline principal.
    """

    def __init__(self):
        """
        Inicializa los rangos HSV para cada color de vehículo.
        Los rangos siguen el espacio de color HSV de OpenCV:
        H: 0-179, S: 0-255, V: 0-255
        Cada color tiene una lista de rangos (lower, upper) porque
        algunos colores como el rojo ocupan dos zonas del espectro HSV.
        """
        self.color_ranges = {
            'rojo':     [
                            (np.array([0, 50, 50]),   np.array([10, 255, 255])),
                            (np.array([165, 50, 50]), np.array([179, 255, 255]))
                        ],
            'naranja':  [(np.array([10, 50, 50]),  np.array([25, 255, 255]))],
            'amarillo': [(np.array([25, 50, 50]),  np.array([35, 255, 255]))],
            'verde':    [(np.array([35, 50, 50]),  np.array([85, 255, 255]))],
            'azul':     [(np.array([100, 50, 50]), np.array([130, 255, 255]))],
            'morado':   [(np.array([130, 50, 50]), np.array([165, 255, 255]))],
            'blanco':   [(np.array([0, 0, 200]),   np.array([179, 30, 255]))],
            'negro':    [(np.array([0, 0, 0]),     np.array([179, 255, 50]))],
            'gris':     [(np.array([0, 0, 50]),    np.array([179, 30, 200]))],
            'plata':    [(np.array([0, 0, 170]),   np.array([179, 25, 220]))],
            'marron':   [(np.array([10, 50, 20]),  np.array([20, 200, 150]))],
        }
        logger.info("Fingerprint inicializado.")

    def is_color_frame(self, frame):
        """
        Comprueba si el frame está en color o en blanco y negro.

        Compara la diferencia entre canales RGB — si los tres canales
        son casi idénticos significa que la cámara está en modo IR/nocturno
        y no tiene sentido intentar clasificar el color.

        Args:
            frame (numpy.ndarray): Frame BGR capturado por la cámara.

        Returns:
            bool: True si el frame tiene color, False si es B&N.
        """
        b, g, r = cv2.split(frame)
        diff_rg = cv2.absdiff(r, g).mean()
        diff_rb = cv2.absdiff(r, b).mean()
        is_color = diff_rg > 5 or diff_rb > 5
        logger.debug(f"Frame en color: {is_color} (diff_rg={diff_rg:.2f}, diff_rb={diff_rb:.2f})")
        return is_color

    def _extract_vehicle_zone(self, frame, plate_y1):
        """
        Extrae la zona de la carrocería del vehículo por encima de la matrícula.

        La carrocería del coche está encima de la matrícula en el frame.
        Usando la coordenada Y superior de la matrícula como referencia,
        se extrae la franja del frame que contiene la carrocería.

        Args:
            frame (numpy.ndarray): Frame completo en formato BGR.
            plate_y1 (int): Coordenada Y superior del bounding box de la matrícula.

        Returns:
            numpy.ndarray: Recorte de la zona de carrocería, o el frame
                           completo si plate_y1 no es válido.
        """
        h = frame.shape[0]
        w = frame.shape[1]


        if plate_y1 is None or plate_y1 <= 0:
            logger.debug("plate_y1 no disponible, usando frame completo para color.")
            return frame

        # Zona de carrocería: entre el 40% superior del frame y la matrícula
        # El 40% evita coger el cielo o el techo del parking
        # El 30% del ancho por el angulo de la camara
        top = max(0, int(h * 0.40))
        right = max(0, int(w*0.30))
        bottom = max(top + 10, plate_y1)
        print(top)
        zone = frame[top:300, right:]

        cv2.imwrite('tests/pruebas/extraccion.jpg', zone)

        logger.debug(f"Zona carrocería extraída: y={top} a y={bottom}")
        return zone

    def detect_color(self, frame, plate_y1=None):
        """
        Detecta el color dominante de la carrocería del vehículo.

        Convierte la zona de carrocería a HSV y aplica las máscaras de
        cada color definido en self.color_ranges. El color con mayor
        número de píxeles coincidentes es el color dominante.

        Args:
            frame (numpy.ndarray): Frame completo en formato BGR.
            plate_y1 (int|None): Coordenada Y superior de la matrícula
                                  para aislar la zona de carrocería.

        Returns:
            str|None: Nombre del color dominante, o None si no se puede
                      determinar con suficiente certeza.
        """
        zone = self._extract_vehicle_zone(frame, plate_y1)
        hsv = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV)

        pixel_counts = {}

        for color_name, ranges in self.color_ranges.items():
            total_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)

            # Algunos colores tienen varios rangos (ej. rojo)
            # acumulamos todas las máscaras del mismo color
            for lower, upper in ranges:
                mask = cv2.inRange(hsv, lower, upper)
                total_mask = cv2.bitwise_or(total_mask, mask)

            pixel_counts[color_name] = cv2.countNonZero(total_mask)

        # Color con más píxeles
        dominant = max(pixel_counts, key=pixel_counts.get)
        total_pixels = zone.shape[0] * zone.shape[1]
        dominant_ratio = pixel_counts[dominant] / total_pixels

        logger.debug(f"Píxeles por color: { {k: v for k, v in sorted(pixel_counts.items(), key=lambda x: x[1], reverse=True)[:3]} }")

        # Si el color dominante cubre menos del 10% de la zona
        # la detección no es fiable
        if dominant_ratio < 0.10:
            logger.debug(f"Color dominante '{dominant}' con ratio {dominant_ratio:.2f} — insuficiente.")
            return None

        logger.info(f"Color detectado: {dominant} ({dominant_ratio:.1%} de la zona)")
        return dominant

    def get_fingerprint(self, frame, plate_y1=None):
        """
        Método principal que genera la huella digital del vehículo.

        Verifica si el frame tiene color y extrae las características
        visuales disponibles. Si la cámara está en modo nocturno B&N
        devuelve la huella con campos None sin intentar clasificar.

        Args:
            frame (numpy.ndarray): Frame completo en formato BGR.
            plate_y1 (int|None): Coordenada Y superior de la matrícula.

        Returns:
            dict: Huella digital con los campos disponibles:
                  {
                    "color": "azul" | None,
                    "is_color_frame": True | False
                  }
        """
        fingerprint = {
            "color": None,
            "is_color_frame": False
        }

        if not self.is_color_frame(frame):
            logger.debug("Frame B&N — huella digital sin color.")
            return fingerprint

        fingerprint["is_color_frame"] = True
        fingerprint["color"] = self.detect_color(frame, plate_y1)

        logger.info(f"Huella digital generada: {fingerprint}")
        return fingerprint