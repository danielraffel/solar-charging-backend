"""Health check endpoint."""

from fastapi import APIRouter
from ..models import HealthResponse
import time

router = APIRouter()

# Track start time for uptime calculation
_start_time = time.time()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    from ..main import app_state

    uptime = time.time() - _start_time if _start_time else None

    return HealthResponse(
        status="ok",
        mqtt_connected=app_state.mqtt.connected if app_state.mqtt else False,
        version="1.0.0",
        uptime_seconds=uptime
    )
