camera_connected = False
eqbroker_connected = False

# Clientes conectados al dashboard. Los actualiza la API (api/routes/websocket.py
# y api/routes/debug.py) y los lee la pipeline para no reducir ni codificar
# vídeo ni recortes cuando no hay nadie mirando.
stream_clients = 0   # conexiones a /ws/camera
stream_fps = 0       # fps más alto que pide alguno de esos clientes
debug_clients = 0    # conexiones a /ws/debug
