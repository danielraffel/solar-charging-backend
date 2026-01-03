"""Charging schedule API endpoints."""

from fastapi import APIRouter, HTTPException
from datetime import datetime
from typing import Optional
import logging

from ..models import (
    ChargeScheduleRequest,
    ChargeScheduleResponse,
    ChargeStatusResponse,
    ScheduleData
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/charge/schedule", response_model=ChargeScheduleResponse)
async def create_schedule(request: ChargeScheduleRequest) -> ChargeScheduleResponse:
    """Create or update a charging schedule."""
    from ..main import app_state

    try:
        # Create schedule data
        schedule = ScheduleData(
            target_soc=request.target_soc,
            start_time=request.start_time,
            mode=request.mode,
            enabled=request.enabled,
            created_at=datetime.now()
        )

        # Set the schedule
        app_state.scheduler.set_schedule(schedule)

        # Save to disk
        app_state.save_schedule(schedule)

        logger.info(f"Schedule created: target={schedule.target_soc}%, start={schedule.start_time}, mode={schedule.mode}")

        return ChargeScheduleResponse(
            target_soc=schedule.target_soc,
            start_time=schedule.start_time,
            mode=schedule.mode,
            enabled=schedule.enabled,
            next_run=schedule.next_run,
            is_charging=app_state.scheduler.is_charging,
            current_soc=app_state.mqtt.current_soc
        )

    except Exception as e:
        logger.error(f"Error creating schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/charge/schedule", response_model=Optional[ChargeScheduleResponse])
async def get_schedule() -> Optional[ChargeScheduleResponse]:
    """Get the current charging schedule."""
    from ..main import app_state

    schedule = app_state.scheduler.current_schedule
    if not schedule:
        return None

    return ChargeScheduleResponse(
        target_soc=schedule.target_soc,
        start_time=schedule.start_time,
        mode=schedule.mode,
        enabled=schedule.enabled,
        next_run=schedule.next_run,
        is_charging=app_state.scheduler.is_charging,
        current_soc=app_state.mqtt.current_soc
    )


@router.delete("/charge/schedule")
async def cancel_schedule():
    """Cancel the current charging schedule."""
    from ..main import app_state

    try:
        app_state.scheduler.cancel_schedule()
        app_state.clear_schedule()

        logger.info("Schedule cancelled")

        return {"success": True, "message": "Schedule cancelled"}

    except Exception as e:
        logger.error(f"Error cancelling schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/charge/status", response_model=ChargeStatusResponse)
async def get_status() -> ChargeStatusResponse:
    """Get current charging status."""
    from ..main import app_state

    schedule = app_state.scheduler.current_schedule

    return ChargeStatusResponse(
        is_charging=app_state.scheduler.is_charging,
        current_soc=app_state.mqtt.current_soc,
        target_soc=schedule.target_soc if schedule else None,
        charging_power=app_state.mqtt.battery_power if app_state.scheduler.is_charging else None,
        scheduled_next=schedule.next_run if schedule else None,
        last_updated=datetime.now()
    )


# =============================================================================
# Phase 2: Immediate Control Endpoints (Backend as Source of Truth)
# =============================================================================

from pydantic import BaseModel, Field


class EnableChargingRequest(BaseModel):
    """Request to enable charging immediately."""
    target_soc: int = Field(..., ge=10, le=100, description="Target state of charge percentage")

    class Config:
        json_schema_extra = {
            "example": {
                "target_soc": 80
            }
        }


class EnableChargingResponse(BaseModel):
    """Response after enabling charging."""
    success: bool
    message: str
    target_soc: int
    current_soc: Optional[int]
    is_charging: bool


class DisableChargingResponse(BaseModel):
    """Response after disabling charging."""
    success: bool
    message: str
    stopped_at: datetime


class ChargingStatusResponse(BaseModel):
    """Comprehensive charging status for app UI."""
    # Charging state
    is_enabled: bool
    is_charging: bool

    # SOC data
    current_soc: Optional[int]
    target_soc: Optional[int]

    # Power data
    charging_power: Optional[float]
    battery_power: Optional[float]

    # Schedule info
    mode: Optional[str]  # "once" or "recurring"
    scheduled_start: Optional[datetime]
    scheduled_next: Optional[datetime]

    # Timestamps
    charge_started_at: Optional[datetime]
    last_updated: datetime

    # MQTT connection status
    mqtt_connected: bool


@router.post("/charging/enable", response_model=EnableChargingResponse)
async def enable_charging(request: EnableChargingRequest) -> EnableChargingResponse:
    """Enable AC charging immediately with target SOC.

    This is the Phase 2 "source of truth" endpoint.
    The backend handles all MQTT publishing - the app just sends intents.
    """
    from ..main import app_state

    try:
        logger.info(f"Enabling AC charging immediately: target={request.target_soc}%")

        # Get current time for immediate start
        now = datetime.now()
        start_time = now.strftime("%H:%M")

        # Calculate end time (start + 8 hours safety cutoff)
        end_hour = (now.hour + 8) % 24
        end_time = f"{end_hour:02d}:{now.minute:02d}"

        # Publish all settings to dongle
        mqtt = app_state.mqtt

        # 1. Enable AC charging
        if not mqtt.publish_ac_charge_enable():
            raise Exception("Failed to enable AC charging")

        # 2. Set time window
        if not mqtt.publish_time_settings(start_time, end_time):
            raise Exception("Failed to set time window")

        # 3. Set ACChgMode=4 (Time+SOC) - critical for SOC-based charging
        if not mqtt.publish_ac_charge_mode(4):
            raise Exception("Failed to set AC charge mode")

        # 4. Set SOC limit
        if not mqtt.publish_soc_limit(request.target_soc):
            raise Exception("Failed to set SOC limit")

        # Update scheduler state
        schedule = ScheduleData(
            target_soc=request.target_soc,
            start_time=start_time,
            mode="once",
            enabled=True,
            created_at=now
        )
        app_state.scheduler.current_schedule = schedule
        app_state.scheduler.is_charging = True
        app_state.scheduler.charge_started_at = now

        # Start SOC monitoring - will auto-stop when target reached
        app_state.scheduler.start_soc_monitoring(schedule)

        logger.info(f"✅ AC charging enabled: target={request.target_soc}%, time={start_time}-{end_time}")
        logger.info(f"   SOC monitoring active - will stop at {request.target_soc}%")

        return EnableChargingResponse(
            success=True,
            message=f"Charging enabled to {request.target_soc}%",
            target_soc=request.target_soc,
            current_soc=mqtt.current_soc,
            is_charging=True
        )

    except Exception as e:
        logger.error(f"Error enabling charging: {e}")
        return EnableChargingResponse(
            success=False,
            message=str(e),
            target_soc=request.target_soc,
            current_soc=app_state.mqtt.current_soc,
            is_charging=False
        )


@router.post("/charging/disable", response_model=DisableChargingResponse)
async def disable_charging() -> DisableChargingResponse:
    """Disable AC charging immediately.

    This is the Phase 2 "source of truth" endpoint.
    The backend handles all MQTT publishing - the app just sends intents.
    """
    from ..main import app_state

    try:
        logger.info("Disabling AC charging")

        # Publish ACCharge=0 to dongle
        if not app_state.mqtt.publish_ac_charge_disable():
            raise Exception("Failed to disable AC charging")

        # Update scheduler state
        app_state.scheduler.is_charging = False
        app_state.scheduler.charge_started_at = None

        # Cancel any scheduled charge
        app_state.scheduler.cancel_schedule()
        app_state.clear_schedule()

        stopped_at = datetime.now()
        logger.info(f"✅ AC charging disabled at {stopped_at}")

        return DisableChargingResponse(
            success=True,
            message="Charging disabled",
            stopped_at=stopped_at
        )

    except Exception as e:
        logger.error(f"Error disabling charging: {e}")
        return DisableChargingResponse(
            success=False,
            message=str(e),
            stopped_at=datetime.now()
        )


@router.get("/charging/status", response_model=ChargingStatusResponse)
async def get_charging_status() -> ChargingStatusResponse:
    """Get comprehensive charging status for app UI.

    This is the Phase 2 "source of truth" endpoint.
    The app uses this to display charging state instead of local settings.
    """
    from ..main import app_state

    schedule = app_state.scheduler.current_schedule
    mqtt = app_state.mqtt

    return ChargingStatusResponse(
        # Charging state
        is_enabled=schedule.enabled if schedule else False,
        is_charging=app_state.scheduler.is_charging,

        # SOC data
        current_soc=mqtt.current_soc,
        target_soc=schedule.target_soc if schedule else None,

        # Power data
        charging_power=mqtt.battery_power if app_state.scheduler.is_charging and mqtt.battery_power and mqtt.battery_power > 0 else None,
        battery_power=mqtt.battery_power,

        # Schedule info
        mode=schedule.mode if schedule else None,
        scheduled_start=None,  # TODO: Parse from start_time if needed
        scheduled_next=schedule.next_run if schedule else None,

        # Timestamps
        charge_started_at=app_state.scheduler.charge_started_at,
        last_updated=datetime.now(),

        # MQTT status
        mqtt_connected=mqtt.connected
    )
