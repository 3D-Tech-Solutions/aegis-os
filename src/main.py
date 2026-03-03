"""Aegis-OS Control Plane API entry point."""

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from src.audit_vault.logger import AuditLogger
from src.control_plane.router import router as control_router

app = FastAPI(
    title="Aegis-OS Control Plane",
    description="The Control Plane for the Enterprise Synthetic Workforce",
    version="0.1.0",
)

# Mount Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# Include routers
app.include_router(control_router, prefix="/api/v1")

_audit_logger = AuditLogger()


@app.on_event("startup")
async def startup_event() -> None:
    _audit_logger.info("aegis.startup", message="Aegis-OS Control Plane starting up")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    _audit_logger.info("aegis.shutdown", message="Aegis-OS Control Plane shutting down")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "aegis-os"}
