"""Device management API endpoints."""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class NotificationPreferences(BaseModel):
    """User preferences for what notifications to receive.

    All preferences default to True for backward compatibility.
    When a device registers without preferences, all notifications are enabled.
    """
    # AC Charging notifications
    ac_charging_notifications: bool = True

    # EVCC notifications
    evcc_mode_change_notifications: bool = True
    evcc_battery_boost_notifications: bool = True
    evcc_live_activities: bool = True


class DeviceRegisterRequest(BaseModel):
    """Request to register a device for push notifications.

    The preferences field is optional for backward compatibility.
    If not provided, all notification types are enabled by default.
    """
    device_token: str
    preferences: Optional[NotificationPreferences] = None


class DeviceRegisterResponse(BaseModel):
    """Response after device registration."""
    success: bool
    message: str


class DeviceStatusResponse(BaseModel):
    """Response with device registration status."""
    apns_enabled: bool
    registered_devices: int


@router.post("/device/register", response_model=DeviceRegisterResponse)
async def register_device(request: DeviceRegisterRequest) -> DeviceRegisterResponse:
    """Register a device token for push notifications.

    The iOS app sends its APNs device token to this endpoint
    to enable push notifications for charging events.
    """
    from ..main import app_state

    if not app_state.apns:
        logger.warning("APNs service not initialized - device registration ignored")
        return DeviceRegisterResponse(
            success=False,
            message="Push notifications not configured on server"
        )

    if not app_state.apns.is_enabled:
        return DeviceRegisterResponse(
            success=False,
            message="Push notifications not enabled"
        )

    # If no preferences provided, use defaults (all enabled)
    preferences = request.preferences if request.preferences else NotificationPreferences()

    success = app_state.apns.register_device(request.device_token, preferences)

    if success:
        logger.info(f"Device registered with preferences: {preferences.model_dump()}")
        return DeviceRegisterResponse(
            success=True,
            message="Device registered for push notifications"
        )
    else:
        return DeviceRegisterResponse(
            success=False,
            message="Invalid device token format"
        )


@router.delete("/device/unregister")
async def unregister_device(request: DeviceRegisterRequest) -> DeviceRegisterResponse:
    """Unregister a device token from push notifications."""
    from ..main import app_state

    if not app_state.apns:
        return DeviceRegisterResponse(
            success=False,
            message="Push notifications not configured"
        )

    success = app_state.apns.unregister_device(request.device_token)

    return DeviceRegisterResponse(
        success=success,
        message="Device unregistered" if success else "Device token not found"
    )


@router.get("/device/status", response_model=DeviceStatusResponse)
async def device_status() -> DeviceStatusResponse:
    """Get push notification service status."""
    from ..main import app_state

    if not app_state.apns:
        return DeviceStatusResponse(
            apns_enabled=False,
            registered_devices=0
        )

    return DeviceStatusResponse(
        apns_enabled=app_state.apns.is_enabled,
        registered_devices=app_state.apns.registered_device_count
    )
