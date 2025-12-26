"""Configuration models for the Solar Charging Backend."""

from pydantic import BaseModel, Field
from typing import Optional


class MQTTConfig(BaseModel):
    """MQTT broker configuration."""
    host: str = Field(..., description="MQTT broker hostname or IP")
    port: int = Field(1883, description="MQTT broker port")
    username: str = Field(..., description="MQTT username")
    password: str = Field(..., description="MQTT password")
    dongle_prefix: str = Field(..., description="Dongle topic prefix (e.g., dongle-XX:XX:XX:XX:XX:XX)")

    class Config:
        json_schema_extra = {
            "example": {
                "host": "192.168.1.100",
                "port": 1883,
                "username": "solar",
                "password": "your-password",
                "dongle_prefix": "dongle-XX:XX:XX:XX:XX:XX"
            }
        }


class ServerConfig(BaseModel):
    """HTTP server configuration."""
    host: str = Field("0.0.0.0", description="Server bind address")
    port: int = Field(8088, description="Server port")


class ChargingConfig(BaseModel):
    """Charging behavior configuration."""
    safety_cutoff_hours: int = Field(8, description="Maximum charging duration (hours)")
    soc_check_interval: int = Field(30, description="Seconds between SOC checks")
    retry_on_failure: bool = Field(True, description="Retry MQTT publish on failure")
    max_retries: int = Field(3, description="Maximum retry attempts")


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = Field("INFO", description="Log level (DEBUG, INFO, WARNING, ERROR)")


class AppConfig(BaseModel):
    """Complete application configuration."""
    mqtt: MQTTConfig
    server: ServerConfig = ServerConfig()
    charging: ChargingConfig = ChargingConfig()
    logging: LoggingConfig = LoggingConfig()
