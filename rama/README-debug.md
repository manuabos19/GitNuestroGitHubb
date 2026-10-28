# RAMA · Dashboard de depuración

Dashboard para pruebas de campo con la cámara del parking. Lo sirve el propio
FastAPI de la RPi, no necesita build ni internet:

    http://IP_RPI:8000/debug

(Si abres el HTML desde otro sitio: `debug.html?host=IP_RPI:8000`)

![dashboard](docs/dashboard-debug.png)

Al pulsar las capturas de un paso de vehículo se abre el visor con la imagen
completa, la matrícula recortada y los datos de la lectura (← → para navegar):

![visor](docs/visor-capturas.png)

## Ficheros

Copiar sobre el proyecto RAMA respetando las rutas:

| Fichero | Cambio |
|---|---|
| `core/telemetry.py` | **Nuevo.** Tiempos, decisiones, sesiones por vehículo y capturas en disco |
| `core/frame_grabber.py` | **Nuevo.** Hilo que lee la cámara continuamente y guarda solo el último frame |
| `core/roi.py` | **Nuevo.** Zona de detección (polígono) |
| `core/tracker.py` | **Nuevo.** Seguimiento de un único vehículo |
| `core/detector_coral_multi.py` | **Nuevo.** Hereda de tu `DetectorCoral` y añade `detect_all()` (todas las matrículas, con NMS). No toca `detector_coral.py` |
| `api/routes/debug.py` | **Nuevo.** `/debug`, `/ws/debug`, `/api/debug/snapshot`, `/api/debug/captures/{fichero}`, `/api/debug/frame.jpg`, `POST /api/debug/detection-config`, `POST /api/debug/reset` |
| `api/static/debug.html` | **Nuevo.** El dashboard (HTML + JS sin dependencias) |
| `api/routes/validacion.py`, `api/static/validacion.html` | **Nuevo.** Página de validación frente a otro sistema (`/validacion`, `GET /api/validacion/lecturas`) |
| `core/pipeline.py` | Último frame (sin `frame_skip`), ROI, seguimiento, OCR con `home_country`, telemetría. **Fingerprint desactivado** |
| `core/validator.py` | Tolera `max_missed_frames`; `finalize()` para decidir al salir; guarda `last_decision` |
| `core/detector_yolo.py` | Devuelve también el bbox; añade `detect_all()` |
| `core/camera.py` | `CAP_PROP_BUFFERSIZE = 1` |
| `core/fingerprint.py` | Corregido pero **sin usar** (lo dejo listo para cuando se active el color) |
| `api/routes/websocket.py`, `api/main.py`, `main.py` | `stream_loop` pasa al loop de uvicorn (antes enviaba desde otro hilo); cada cliente elige sus fps (`/ws/camera?fps=N`) |
| `core/state.py` | Número de clientes del vídeo y del dashboard: sin nadie mirando no se prepara vídeo ni recortes |
| `api/routes/settings.py` | `GET /api/settings` devuelve la contraseña de la cámara como `********`; el `POST` ignora ese valor |

## Configuración nueva (opcional, `settings.yaml`)

Todo tiene valor por defecto; no hace falta tocar el yaml para que funcione.
La ROI y el seguimiento se configuran mejor desde el dashboard: se aplican al
momento y se guardan en `settings.yaml`.

```yaml
site_name: "Parking Centro - Entrada 1"   # nombre en el dashboard y en el panel general

detection:
  max_missed_frames: 2      # frames seguidos sin detección antes de dar el vehículo por ido
  roi:
    enabled: false
    polygon: []             # [[x, y], ...] normalizados 0-1; se dibuja en /debug

tracking:
  enabled: false
  skip_ocr_after_read: true # no volver a pasar el OCR a un vehículo ya leído
  decide_on_exit: true      # si se va sin completar el voting, decidir con lo que haya
  min_frames_on_exit: 2     # lecturas mínimas para decidir al salir
  max_jump: 2.0             # desplazamiento máximo entre frames (anchos de matrícula)
  switch_ratio: 1.3         # cambiar a una matrícula X veces más ancha (más cercana); 0 = nunca
  switch_frames: 3          # durante cuántos frames seguidos

stream:                     # solo el vídeo del dashboard, NO afecta a la detección
  width: 320
  height: 180
  fps: 10                   # máximo; cada cliente puede pedir menos con ?fps=N

debug:
  captures_path: debug_captures   # capturas + sesiones.jsonl
  max_captures: 1000              # ficheros jpg máximos (se borran los más antiguos)
```

## Para el panel general

- **Sin nadie mirando no se gasta CPU en vídeo**: la RPi solo reduce y
  codifica frames si hay alguien conectado a `/ws/camera`, y solo a los fps
  que pida (el más alto de los clientes). Cada frame se codifica una vez,
  mire quien mire. Los recortes del dashboard en vivo igual: solo si hay
  alguien en `/ws/debug`. Las capturas de cada vehículo se guardan siempre.
- **`GET /api/debug/summary`**: resumen ligero para las tarjetas (nombre del
  parking, estado de cámara y EQbroker, fps, retraso, % leídos, última
  lectura con sus capturas, vehículo presente, temperatura, quién mira).
- **`/debug` funciona bajo una ruta**: abierto como
  `https://panel/camara/parking1/debug` usa `/camara/parking1/api/...` y
  `/camara/parking1/ws/...`, así el panel general puede servirlo a través
  de un proxy sin cambiar nada. `?fps=N` fija los fps del vídeo.

## Validación frente a otro sistema (`/validacion`)

`http://IP_RPI:8000/validacion` (o el enlace «Validación» del dashboard).

1. Elige el rango de fechas.
2. «Elegir carpeta…»: la carpeta de imágenes del otro sistema (normalmente
   `C:\matriculas`). **La lee el navegador del equipo donde está**, la RPi no
   necesita acceso a ella y no se sube nada. En Chrome/Edge sobre https o
   localhost la carpeta se recuerda; sobre `http://IP_RPI` hay que elegirla
   cada vez. Se leen también las subcarpetas.
3. Nombres esperados: `S3,CAM4,260627010336569,3471LXM,9637.jpg` → sitio,
   cámara, fecha `AAMMDDhhmmssmmm`, matrícula, resto. Los ficheros con otro
   formato se ignoran (se indica cuántos). Se puede filtrar por sitio y cámara.
4. «Validar». Para cada lectura aceptada por RAMA en el rango
   (`sesiones.jsonl`, `GET /api/validacion/lecturas`):
   - **Coincidencias**: existe un fichero con la misma matrícula. Si una
     matrícula pasa varias veces se emparejan una a una por cercanía en el tiempo.
   - **Sin coincidencia exacta**: se busca entre las lecturas del otro sistema
     que quedaron sueltas la más parecida con distancia de Levenshtein ≤ 2
     (configurable) → «posible misma matrícula», con las fotos de los dos
     sistemas y los caracteres distintos marcados. Las que no tienen ninguna
     parecida quedan como «sin pareja».
   - **Solo en el otro sistema**: lecturas del otro sistema sin ninguna de
     RAMA parecida (posibles vehículos que RAMA no leyó).
5. En cada posible se marca cuál está bien (RAMA / otro / las dos mal / no es
   el mismo vehículo). Se guarda en el navegador y sale en el CSV.

Opcional: «ventana de tiempo máxima» para emparejar solo lecturas separadas
como mucho N minutos (0 = solo por matrícula dentro del rango).

Las fotos de RAMA son las capturas de `debug_captures/`; las más antiguas se
borran al pasar de `debug.max_captures` (1000 por defecto). Para validar
rangos largos con fotos, sube ese valor.

## Zona de detección (ROI)

En `/debug` → «Nueva foto», se dibuja el polígono sobre la imagen real (clic
para añadir puntos, arrastrar para moverlos, doble clic para borrar) y
«Guardar y aplicar». El detector solo recibe el rectángulo que contiene la
zona, con lo de fuera en negro:
- Las matrículas fuera (la calle, el carril de al lado) no se detectan.
- La matrícula ocupa más píxeles en la entrada 640×640 de la Coral, así que
  se detecta mejor y desde más lejos.

## Seguimiento del vehículo

Se engancha a la matrícula más grande (el vehículo más cercano) y en los
frames siguientes solo acepta la que sea su continuación (posición y tamaño
coherentes). Las demás se dibujan en naranja y se ignoran hasta que el
vehículo seguido desaparece `max_missed_frames` frames. Mejoras sobre el voting:
- Al empezar un vehículo nuevo se resetea el voting: nunca se mezclan votos
  de dos coches.
- Leído un vehículo, no se le vuelve a pasar el OCR (el OCR es lo más caro).
- Si el vehículo se va antes de completar `frames_for_voting`, se decide con
  las lecturas que haya (mínimo `min_frames_on_exit` y mismo consenso de
  Levenshtein). Aparece como «al salir» en la tabla.
- Si un coche parado o el de detrás se engancha primero y luego entra otro
  claramente más cerca, se cambia a ese.

## Tiempo real

Antes el pipeline leía todos los frames y procesaba 1 de cada 3. Si la
inferencia tardaba más que 3 frames de cámara, OpenCV acumulaba frames del
RTSP y el retraso crecía hasta segundos. Ahora un hilo vacía el stream
continuamente y el pipeline siempre coge el más reciente; los intermedios se
descartan (contador *frames descartados*).

En el dashboard:
- **Retraso real (captura → resultado):** lo que tarda un frame desde que llega
  de la cámara hasta tener la lectura. Se marca en rojo si el p95 pasa de 1 s.
- **FPS cámara:** si es bastante menor que `camera.fps`, la RPi no decodifica el
  RTSP a tiempo (aviso en rojo). Ahí sí compensa bajar la resolución o usar el
  substream de la cámara.

## Qué se guarda de cada vehículo

Una sesión empieza con la primera detección y termina tras más de
`max_missed_frames` frames sin detección. Al cerrarse se guardan en
`debug_captures/`:
- `<fecha>_s<id>_full.jpg`: el frame completo con el bbox y la lectura dibujados.
- `<fecha>_s<id>_crop.jpg`: el recorte que recibió el OCR.
- Una línea en `sesiones.jsonl` con todos los datos (se recarga al reiniciar).

La captura es la del momento en que se aceptó la matrícula. Si no se llegó a
leer, es la del frame con mayor confianza del detector.
