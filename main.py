"""Main entry point for Solar Charging Backend."""

import os
from pathlib import Path
import yaml
from app.models import AppConfig

if __name__ == "__main__":
    import uvicorn

    # Load config to get port
    config_path = Path("config.yaml")
    if not config_path.exists():
        print("ERROR: config.yaml not found! Please create it from config.example.yaml")
        exit(1)

    with open(config_path) as f:
        config_data = yaml.safe_load(f)

    config = AppConfig(**config_data)

    # Override port from environment variable if set
    port = int(os.getenv("SERVER_PORT", config.server.port))
    host = os.getenv("SERVER_HOST", config.server.host)

    print(f"Starting server on {host}:{port}")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info"
    )
