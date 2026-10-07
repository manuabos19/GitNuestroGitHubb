from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import detections, state, settings, websocket, debug

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────────────
# Permite peticiones desde el frontend Quasar en desarrollo.
# allow_credentials=True es necesario para que el navegador envíe
# la cookie de sesión en cada petición cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:9000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(detections.router, prefix="/api")
app.include_router(state.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(websocket.router)
app.include_router(debug.router)


@app.on_event("startup")
async def startup():
    # El broadcaster de telemetría corre en el loop de uvicorn
    debug.start_broadcaster()
