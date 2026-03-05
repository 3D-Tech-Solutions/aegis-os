# Copyright 2026 Tim Escolopio / 3D Tech Solutions
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Aegis-OS Control Plane API entry point."""

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import make_asgi_app

from src.audit_vault.logger import AuditLogger
from src.control_plane.router import router as control_router

# ---------------------------------------------------------------------------
# OpenTelemetry provider setup
# Must be configured before any AuditLogger or tracer is created.
# In production, replace ConsoleSpanExporter with an OTLP exporter via
# environment-variable-driven SDK auto-configuration.
# ---------------------------------------------------------------------------
_otel_provider = TracerProvider()
_otel_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(_otel_provider)

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
