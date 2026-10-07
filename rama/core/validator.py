from config.config import config
import logging
from Levenshtein import distance
import re

logger = logging.getLogger(__name__)

class Validator:
  def __init__(self):
      self.votes = {}        # { "6630FGV": 1.87 }
      self.cooldown = {}     # { "6630FGV": timestamp }
      self.frame_count = 0
      self.missed = 0        # frames seguidos sin detección
      self.last_decision = None  # motivo de la última decisión (para el dashboard de depuración)

  def validate(self, texto, confianza, vehiculo_presente=True):
      """
      Método principal del validator. Procesa una lectura OCR aplicando
      todas las capas de validación en orden.

      Args:
          texto (str): Texto leído por el OCR.
          confianza (float): Confianza de la lectura entre 0 y 1.
          vehiculo_presente (bool): Si el detector ve un vehículo en el frame.
              Un fallo puntual del detector no resetea el voting: solo se
              resetea tras más de detection.max_missed_frames frames seguidos
              sin detección (por defecto 2).

      Returns:
          str: Matrícula aceptada o None si no hay resultado todavía.
      """
      # Reset si el vehículo desapareció (tolerando fallos puntuales del detector)
      if not vehiculo_presente:
          self.missed += 1
          if self.missed > config['detection'].get('max_missed_frames', 2) and self.votes:
              logger.debug(f"Vehículo perdido tras {self.missed} frames sin detección.")
              self.reset()
          return None
      self.missed = 0

      # Detección sin lectura OCR: el vehículo sigue ahí pero no hay voto
      if texto is None:
          self._set_decision('ocr_vacio', None)
          return None

      # Capa 1: filtro threshold
      if confianza < config['ocr']['threshold']:
          logger.debug(f"Lectura descartada por confianza baja: {confianza:.2f}")
          self._set_decision('baja_confianza', texto)
          return None

      # Capa 2: limpieza
      texto = self._clean_text(texto)

      # Capa 3: identificación de país
      pais = self._identify_country(texto)

      # Capa 4: corrección de caracteres
      texto = self._correct_chars(texto, pais)

      # Capa 5: añadir al voting
      self._add_to_voting(texto, confianza)

      # ¿Tenemos suficientes frames?
      if self.frame_count < config['detection']['frames_for_voting']:
          self._set_decision('acumulando', texto)
          return None

      # Capa 6: decisión final con Levenshtein
      ganador = self._get_winner()
      if ganador is None:
          self._set_decision('sin_consenso', texto)
          return None

      # Capa 7: cooldown
      if self._check_cooldown(ganador):
          self._set_decision('cooldown', texto, ganador)
          return None

      self._set_cooldown(ganador)
      self._set_decision('aceptada', texto, ganador)
      self.reset()
      logger.info(f"Matrícula aceptada: {ganador}")
      return ganador

  def _set_decision(self, decision, texto, ganador=None):
      """
      Guarda el motivo de la última decisión del validator, con una copia
      del voting en ese momento. Solo informativo, para depuración.

      Args:
          decision (str): ocr_vacio | baja_confianza | acumulando | sin_consenso | cooldown | aceptada.
          texto (str): Texto de la lectura tras limpieza/corrección.
          ganador (str|None): Matrícula ganadora si la hay.
      """
      self.last_decision = {
          "decision": decision,
          "text": texto,
          "winner": ganador,
          "votes": {t: round(float(p), 3) for t, p in self.votes.items()},
          "frame_count": self.frame_count,
          "frames_needed": config['detection']['frames_for_voting'],
      }

  def _clean_text(self, texto):
      """Normaliza el texto eliminando caracteres no deseados y convirtiendo a mayúsculas."""
      return texto.upper().strip().replace(' ', '').replace('-', '').replace('.', '')

  def _identify_country(self, texto):
    """
    Identifica el país de la instalación desde la configuración.
    Solo devuelve el país si está entre los soportados.

    Args:
        texto (str): No usado directamente, el país viene de settings.yaml.

    Returns:
        str: Código de país (ES, FR, DE, PT, IT) o None si no está soportado.
    """
    country = config.get('location', {}).get('country', None)
    
    if country in ["ES", "FR", "DE", "PT", "IT"]:
        logger.debug(f"País identificado: {country}")
        return country

    logger.debug(f"País '{country}' no soportado, sin corrección de caracteres.")
    return None

  def _correct_chars(self, texto, pais):
    """
    Corrige caracteres confusos según el país identificado.
    Solo aplica correcciones si el texto coincide con el patrón del país.
    Si no coincide o el país es None devuelve el texto sin modificar.

    Args:
        texto (str): Texto leído por el OCR.
        pais (str): Código de país identificado (ES, FR, DE, PT, IT) o None.

    Returns:
        str: Texto corregido o el mismo texto si no aplica corrección.
    """
    if pais is None:
        return texto

    patrones = {
        'ES': r'^\d{4}[A-Z]{3}$',
        'FR': r'^[A-Z]{2}-\d{3}-[A-Z]{2}$',
        'DE': r'^[A-Z]{1,3}\s[A-Z]{1,2}\s\d{1,4}$',
        'PT': r'^[A-Z]{2}-\d{2}-[A-Z]{2}$',
        'IT': r'^[A-Z]{2}\d{3}[A-Z]{2}$',
    }

    # Caracteres que parecen números pero son letras y viceversa
    a_numero = str.maketrans('OIBSZ', '01852')
    a_letra  = str.maketrans('01852', 'OIBSZ')

    texto = texto.upper().replace(' ', '').replace('-', '')

    if pais == 'ES':
        # Formato: 4 números + 3 letras → NNNNLLL
        parte_num = texto[:4].translate(a_numero)
        parte_let = texto[4:].translate(a_letra)
        corregido = parte_num + parte_let

    elif pais == 'FR':
        # Formato: LL-NNN-LL → letras-números-letras
        partes = texto.split('-') if '-' in texto else [texto[:2], texto[2:5], texto[5:]]
        corregido = '-'.join([
            partes[0].translate(a_letra),
            partes[1].translate(a_numero),
            partes[2].translate(a_letra)
        ])

    elif pais == 'PT':
        # Formato: LL-NN-LL → letras-números-letras
        partes = texto.split('-') if '-' in texto else [texto[:2], texto[2:4], texto[4:]]
        corregido = '-'.join([
            partes[0].translate(a_letra),
            partes[1].translate(a_numero),
            partes[2].translate(a_letra)
        ])

    elif pais == 'IT':
        # Formato: LLNNNLL → letras-números-letras
        corregido = (
            texto[:2].translate(a_letra) +
            texto[2:5].translate(a_numero) +
            texto[5:].translate(a_letra)
        )

    elif pais == 'DE':
        # Formato variable, solo corregimos la parte numérica al final
        partes = texto.split()
        if len(partes) >= 2:
            corregido = ' '.join(partes[:-1]) + ' ' + partes[-1].translate(a_numero)
        else:
            corregido = texto
    else:
        return texto

    # Verificar que el resultado corregido sigue coincidiendo con el patrón
    if re.match(patrones[pais], corregido):
        logger.debug(f"Corrección aplicada [{pais}]: {texto} → {corregido}")
        return corregido

    # Si tras corregir no coincide con el patrón, devolver original
    logger.debug(f"Corrección descartada [{pais}]: {corregido} no coincide con patrón")
    return texto
      

      
  def _add_to_voting(self, texto, confianza):
    """
    Añade una lectura al voting ponderado.

    Si el texto ya existe acumula la confianza como peso adicional.
    Si es nuevo lo añade con su confianza como peso inicial.
    Incrementa el contador de frames en cada llamada.

    Args:
        texto (str): Texto de la matrícula ya limpio y corregido.
        confianza (float): Confianza de la lectura entre 0 y 1.
    """
    if texto in self.votes:
        self.votes[texto] += confianza
    else:
        self.votes[texto] = confianza

    self.frame_count += 1
    

  def _get_winner(self):
      """
      Aplica fusión Levenshtein y decide la matrícula ganadora.

      Fusiona lecturas similares (distancia <= 1), selecciona la de
      mayor peso acumulado y verifica que el consenso es suficiente.

      Returns:
          str: Matrícula ganadora o None si no hay consenso suficiente.
      """
      if not self.votes:
          return None


      # Copia para no modificar self.votes mientras iteramos
      merged = dict(self.votes)

      # Comparar todos contra todos y fusionar similares
      textos = list(merged.keys())
      for i in range(len(textos)):
          for j in range(i + 1, len(textos)):
              t1, t2 = textos[i], textos[j]
              if t1 in merged and t2 in merged:
                  if distance(t1, t2) <= 1:
                      # Fusionar el de menor peso en el de mayor peso
                      if merged[t1] >= merged[t2]:
                          merged[t1] += merged[t2]
                          del merged[t2]
                      else:
                          merged[t2] += merged[t1]
                          del merged[t1]

      # Ordenar por peso descendente
      ordenado = sorted(merged.items(), key=lambda x: x[1], reverse=True)
      ganador = ordenado[0][0]
      peso_ganador = ordenado[0][1]

      # Comprobar diferencia con el segundo si existe
      if len(ordenado) > 1:
          peso_segundo = ordenado[1][1]
          if peso_ganador - peso_segundo < 0.3:
              logger.debug("Consenso insuficiente, necesita más frames.")
              return None

      logger.debug(f"Ganador: {ganador} con peso {peso_ganador:.2f}")
      return ganador


  def _check_cooldown(self, matricula):
      """
      Comprueba si una matrícula está en periodo de cooldown.

      Args:
          matricula (str): Matrícula a comprobar.

      Returns:
          bool: True si está en cooldown y debe ignorarse, False si puede procesarse.
      """
      import time
      cooldown_segundos = config['detection'].get('cooldown_seconds', 10)

      if matricula in self.cooldown:
          elapsed = time.time() - self.cooldown[matricula]
          if elapsed < cooldown_segundos:
              logger.debug(f"Matrícula {matricula} en cooldown ({elapsed:.1f}s)")
              return True
          else:
              del self.cooldown[matricula]

      return False


  def _set_cooldown(self, matricula):
      """
      Marca una matrícula como procesada iniciando su cooldown.

      Args:
          matricula (str): Matrícula a marcar.
      """
      import time
      self.cooldown[matricula] = time.time()
      logger.debug(f"Cooldown iniciado para {matricula}")


  def reset(self):
      """
      Limpia el voting acumulado cuando el vehículo desaparece del frame.
      No limpia el cooldown ya que debe persistir entre vehículos.
      """
      self.votes = {}
      self.frame_count = 0
      self.missed = 0
      logger.debug("Voting reseteado.")