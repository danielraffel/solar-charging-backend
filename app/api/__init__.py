"""API endpoints."""

from .health import router as health_router
from .charge import router as charge_router
from .device import router as device_router

__all__ = ["health_router", "charge_router", "device_router"]
