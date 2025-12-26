"""Schedule and charging state models."""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime
import re


class ChargeScheduleRequest(BaseModel):
    """Request to create/update a charging schedule."""
    target_soc: int = Field(..., ge=10, le=100, description="Target state of charge percentage")
    start_time: str = Field(..., description="Start time in HH:MM format (24-hour)")
    mode: Literal["once", "recurring"] = Field("recurring", description="Schedule mode")
    enabled: bool = Field(True, description="Whether schedule is enabled")

    @field_validator("start_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time is in HH:MM format."""
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", v):
            raise ValueError("start_time must be in HH:MM format (00:00 to 23:59)")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "target_soc": 85,
                "start_time": "02:30",
                "mode": "recurring",
                "enabled": True
            }
        }


class ChargeScheduleResponse(BaseModel):
    """Response with schedule details."""
    target_soc: int
    start_time: str
    mode: str
    enabled: bool
    next_run: Optional[datetime] = None
    is_charging: bool = False
    current_soc: Optional[int] = None


class ChargeStatusResponse(BaseModel):
    """Current charging status."""
    is_charging: bool
    current_soc: Optional[int]
    target_soc: Optional[int]
    charging_power: Optional[float]
    scheduled_next: Optional[datetime]
    last_updated: datetime


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    mqtt_connected: bool
    version: str
    uptime_seconds: Optional[float]


class ScheduleData(BaseModel):
    """Internal schedule data model (for persistence)."""
    target_soc: int
    start_time: str
    mode: Literal["once", "recurring"]
    enabled: bool
    created_at: datetime
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
