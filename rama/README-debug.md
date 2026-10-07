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
| `api/routes/debug.py` | **Nuevo.** `/debug`, `/ws/debug`, `/api/debug/snapshot`, `/api/debug/captures/{fichero}`, `POST /api/debug/reset` |
| `api/static/debug.html` | **Nuevo.** El dashboard (HTML + JS sin dependencias) |
| `core/pipeline.py` | Procesa siempre el último frame (sin `frame_skip`), mide cada etapa, OCR con `home_country`, fingerprint con la posición de la matrícula |
| `core/validator.py` | Tolera `max_missed_frames` frames sin detección antes de resetear el voting; guarda `last_decision` |
| `core/fingerprint.py` | Zona de carrocería corregida (`frame[top:300]` salía vacía en 1080p), sin `print` ni `imwrite` |
| `core/camera.py` | `CAP_PROP_BUFFERSIZE = 1` |
| `core/detector_yolo.py` | Devuelve también el bbox, como `DetectorCoral` |
| `api/routes/websocket.py`, `api/main.py`, `main.py` | `stream_loop` pasa al loop de uvicorn (antes enviaba desde otro hilo) |

## Configuración nueva (opcional, `settings.yaml`)

Todo tiene valor por defecto; no hace falta tocar el yaml para que funcione.

```yaml
detection:
  max_missed_frames: 2      # frames seguidos sin detección antes de dar el vehículo por perdido

stream:                     # solo el vídeo del dashboard, NO afecta a la detección
  width: 320
  height: 180
  fps: 10

debug:
  captures_path: debug_captures   # capturas + sesiones.jsonl
  max_captures: 1000              # ficheros jpg máximos (se borran los más antiguos)
```

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
