"""Main application entry point."""

import logging
import sys
import json
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import yaml

from .models import AppConfig, ScheduleData
from .mqtt import MQTTClient
from .scheduler import ChargingScheduleManager
from .api import health_router, charge_router


# Configure logging
def setup_logging(level: str = "INFO"):
    """Configure logging for the application."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )


logger = logging.getLogger(__name__)


# Application state
class AppState:
    """Global application state."""

    def __init__(self):
        self.config: Optional[AppConfig] = None
        self.mqtt: Optional[MQTTClient] = None
        self.scheduler: Optional[ChargingScheduleManager] = None
        self.data_dir = Path("data")
        self.schedule_file = self.data_dir / "schedule.json"

    def save_schedule(self, schedule: ScheduleData):
        """Save schedule to disk."""
        self.data_dir.mkdir(exist_ok=True)
        with open(self.schedule_file, "w") as f:
            json.dump(schedule.model_dump(mode="json"), f, indent=2, default=str)
        logger.debug(f"Schedule saved to {self.schedule_file}")

    def load_schedule(self) -> Optional[ScheduleData]:
        """Load schedule from disk."""
        if not self.schedule_file.exists():
            return None

        try:
            with open(self.schedule_file, "r") as f:
                data = json.load(f)
                return ScheduleData(**data)
        except Exception as e:
            logger.error(f"Failed to load schedule: {e}")
            return None

    def clear_schedule(self):
        """Clear saved schedule."""
        if self.schedule_file.exists():
            self.schedule_file.unlink()
            logger.debug("Schedule file deleted")


app_state = AppState()


# Lifecycle management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Solar Charging Backend...")

    # Load configuration
    config_path = Path("config.yaml")
    if not config_path.exists():
        logger.error("config.yaml not found! Please create it from config.example.yaml")
        sys.exit(1)

    with open(config_path) as f:
        config_data = yaml.safe_load(f)

    app_state.config = AppConfig(**config_data)

    # Override port from environment variable if set
    if server_port := os.getenv("SERVER_PORT"):
        try:
            app_state.config.server.port = int(server_port)
            logger.info(f"Port overridden by SERVER_PORT environment variable: {server_port}")
        except ValueError:
            logger.warning(f"Invalid SERVER_PORT value '{server_port}', using config file value")

    setup_logging(app_state.config.logging.level)

    logger.info(f"Loaded configuration from {config_path}")
    logger.info(f"Server will run on {app_state.config.server.host}:{app_state.config.server.port}")

    # Initialize MQTT client
    app_state.mqtt = MQTTClient(app_state.config.mqtt)
    if not app_state.mqtt.connect():
        logger.error("Failed to connect to MQTT broker")
        sys.exit(1)

    # Initialize scheduler
    app_state.scheduler = ChargingScheduleManager(
        app_state.mqtt,
        app_state.config.charging
    )
    app_state.scheduler.start()

    # Load and restore any saved schedule
    saved_schedule = app_state.load_schedule()
    if saved_schedule:
        logger.info(f"Restoring saved schedule: {saved_schedule}")
        app_state.scheduler.set_schedule(saved_schedule)

    logger.info("Solar Charging Backend started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Solar Charging Backend...")
    app_state.scheduler.stop()
    app_state.mqtt.disconnect()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Solar Charging Backend",
    description="Backend service for managing solar battery charging schedules",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware (allow iOS app to connect)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to iOS app's origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(charge_router, prefix="/api", tags=["charging"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Solar Charging Backend",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    import os

    # Load config to get port (fallback entry point)
    config_path = Path("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
        config = AppConfig(**config_data)
        port = int(os.getenv("SERVER_PORT", config.server.port))
        host = os.getenv("SERVER_HOST", config.server.host)
    else:
        port = int(os.getenv("SERVER_PORT", 8088))
        host = os.getenv("SERVER_HOST", "0.0.0.0")
        print("WARNING: config.yaml not found, using defaults")

    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
