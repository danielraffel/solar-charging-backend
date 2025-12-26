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
