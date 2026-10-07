import asyncio
import logging
import queue
import threading
import uvicorn

from core.eqbroker import EQbrokerClient
from core.pipeline import Pipeline
from api.routes.websocket import stream_loop


def setup_logging():
    """
    Configura el sistema de logging para toda la aplicación.
    Nivel y formato definidos aquí afectan a todos los módulos
    que usen logging.getLogger(__name__).
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def run_uvicorn():
    """
    Arranca el servidor uvicorn con la API FastAPI en un hilo separado.

    Escucha en todas las interfaces en el puerto 8000, accesible
    desde el dashboard en http://IP_RPI:8000.
    """
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False)


def run_asyncio_loop(loop: asyncio.AbstractEventLoop):
    """
    Arranca el event loop de asyncio en el hilo secundario.

    Esta función es bloqueante — se queda corriendo indefinidamente
    hasta que alguien llame a loop.stop(). Es el motor que ejecuta
    todas las corrutinas de EQbroker (testigo, discovery, consumir_cola)
    y del stream de cámara (stream_loop).

    Args:
        loop: Event loop de asyncio creado en el hilo principal.
    """
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def consumir_cola(cola: queue.Queue, eqbroker: EQbrokerClient):
    """
    Corrutina que consume eventos de la cola y los publica en EQbroker.

    Corre indefinidamente dentro del event loop de asyncio. Cuando
    la cola está vacía espera 100ms antes de volver a mirar, para no
    consumir CPU innecesariamente. Cuando llega un evento lo publica
    en EQbroker sin bloquear el pipeline de detección.

    Args:
        cola: Cola compartida con el pipeline. El pipeline mete eventos,
              esta corrutina los saca y los publica.
        eqbroker: Cliente EQbroker para publicar los eventos.
    """
    logger = logging.getLogger(__name__)

    while True:
        try:
            evento = cola.get_nowait()
            logger.debug(f"Evento recibido de la cola: {evento}")
            await eqbroker.publicar_deteccion(evento)
        except queue.Empty:
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error publicando evento en EQbroker: {e}")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    """
    Punto de entrada principal del sistema RAMA.

    Arranca tres hilos en paralelo:
      - Hilo principal:   pipeline síncrono (cámara → detector → OCR → validator)
      - Hilo asyncio:     event loop asyncio (EQbroker, heartbeat, discovery, stream)
      - Hilo uvicorn:     servidor FastAPI (API REST + WebSocket /ws/camera)

    La comunicación entre hilos se hace a través de dos queue.Queue:
      - cola:        el pipeline mete eventos de detección, asyncio los publica en EQbroker
      - cola_stream: el pipeline mete frames redimensionados, asyncio los
                     envía por WebSocket a los clientes del dashboard
    """
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Iniciando sistema RAMA...")

    # Cola compartida entre pipeline (productor) y asyncio (consumidor) — EQbroker
    cola = queue.Queue(maxsize=100)

    # Cola compartida para el websocket y el stream de video
    cola_stream = queue.Queue(maxsize=10)

    # Crear el event loop de asyncio manualmente para controlarlo
    # desde un hilo separado
    loop = asyncio.new_event_loop()

    # Hilo asyncio: arranca el event loop
    # daemon=True significa que se mata solo cuando el hilo principal termina
    hilo_asyncio = threading.Thread(
        target=run_asyncio_loop,
        args=(loop,),
        daemon=True,
        name="hilo-asyncio"
    )
    hilo_asyncio.start()
    logger.info("Hilo asyncio arrancado.")

    # Instanciar EQbroker y lanzar sus corrutinas en el loop
    eqbroker = EQbrokerClient()
    asyncio.run_coroutine_threadsafe(eqbroker.iniciar(), loop)
    asyncio.run_coroutine_threadsafe(consumir_cola(cola, eqbroker), loop)

    # Lanzar el stream de cámara para el dashboard
    asyncio.run_coroutine_threadsafe(stream_loop(cola_stream), loop)
    logger.info("EQbroker, consumidor de cola y stream de cámara lanzados.")

    # Hilo uvicorn: API REST + WebSocket
    hilo_uvicorn = threading.Thread(
        target=run_uvicorn,
        daemon=True,
        name="hilo-uvicorn"
    )
    hilo_uvicorn.start()
    logger.info("Hilo uvicorn arrancado — API disponible en http://0.0.0.0:8000")

    # Pipeline síncrono en el hilo principal
    pipeline = Pipeline(cola, cola_stream)

    try:
        pipeline.start()
    except KeyboardInterrupt:
        logger.info("Señal de parada recibida (Ctrl+C).")
    finally:
        pipeline.stop()
        loop.call_soon_threadsafe(loop.stop)
        hilo_asyncio.join(timeout=3)
        logger.info("Sistema RAMA detenido.")