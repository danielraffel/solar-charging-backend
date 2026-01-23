"""Apple Push Notification service for iOS app notifications."""

import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict

from aioapns import APNs, NotificationRequest, PushType

from ..models import APNsConfig

logger = logging.getLogger(__name__)

@dataclass
class DeviceInfo:
    """Information about a registered device including its notification preferences.

    Attributes:
        token: APNs device token (hex string)
        preferences: User's notification preferences from device.py NotificationPreferences
    """
    token: str
    # Store preferences as a dict to avoid circular import
    # Structure: {
    #   'ac_charging_notifications': bool,
    #   'evcc_mode_change_notifications': bool,
    #   'evcc_battery_boost_notifications': bool,
    #   'evcc_live_activities': bool
    # }
    preferences: dict

    def __hash__(self):
        """Make DeviceInfo hashable based on token."""
        return hash(self.token)

    def __eq__(self, other):
        """Compare DeviceInfo instances by token."""
        if isinstance(other, DeviceInfo):
            return self.token == other.token
        return False



class APNsService:
    """Service for sending push notifications to iOS devices."""

    def __init__(self, config: APNsConfig):
        """Initialize APNs service with configuration.

        Args:
            config: APNs configuration with key path, key ID, team ID, and bundle ID
        """
        self.config = config
        self.client: Optional[APNs] = None
        # Changed from Set[str] to Dict[str, DeviceInfo] to store preferences
        self.devices: Dict[str, DeviceInfo] = {}
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize APNs client. Call this during app startup.

        Returns:
            True if initialization successful, False otherwise
        """
        if not self.config.enabled:
            logger.info("APNs is disabled in configuration")
            return False

        # Validate configuration
        if not self.config.key_path:
            logger.error("APNs key_path not configured")
            return False

        key_file = Path(self.config.key_path)
        if not key_file.exists():
            logger.error(f"APNs key file not found: {self.config.key_path}")
            return False

        if not self.config.key_id or not self.config.team_id:
            logger.error("APNs key_id and team_id must be configured")
            return False

        if not self.config.bundle_id:
            logger.error("APNs bundle_id must be configured")
            return False

        try:
            self.client = APNs(
                key=str(key_file),
                key_id=self.config.key_id,
                team_id=self.config.team_id,
                topic=self.config.bundle_id,
                use_sandbox=self.config.use_sandbox,
            )
            self._initialized = True
            logger.info(f"APNs service initialized (sandbox={self.config.use_sandbox})")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize APNs client: {e}")
            return False

    def register_device(self, device_token: str, preferences) -> bool:
        """Register a device token for push notifications with user preferences.

        Args:
            device_token: Hex-encoded device token from iOS
            preferences: NotificationPreferences object from device.py

        Returns:
            True if registration successful
        """
        if not device_token:
            logger.warning("Empty device token received")
            return False

        # Validate token format (hex string, typically 64 characters)
        if not all(c in '0123456789abcdef' for c in device_token.lower()):
            logger.warning(f"Invalid device token format: {device_token[:20]}...")
            return False

        # Convert preferences to dict to avoid circular import
        prefs_dict = preferences.model_dump() if hasattr(preferences, 'model_dump') else preferences.dict()

        # Store device with preferences
        device_info = DeviceInfo(token=device_token, preferences=prefs_dict)
        self.devices[device_token] = device_info

        logger.info(
            f"Registered device token: {device_token[:20]}... "
            f"(total: {len(self.devices)}, preferences: {prefs_dict})"
        )
        return True

    def unregister_device(self, device_token: str) -> bool:
        """Unregister a device token.

        Args:
            device_token: Device token to remove

        Returns:
            True if token was found and removed
        """
        if device_token in self.devices:
            del self.devices[device_token]
            logger.info(f"Unregistered device token: {device_token[:20]}...")
            return True
        return False

    def _should_send_notification(self, device_info: DeviceInfo, notification_type: str) -> bool:
        """Check if a notification should be sent to a device based on preferences.

        Args:
            device_info: Device information with preferences
            notification_type: Type of notification (e.g., 'evcc_mode_changed')

        Returns:
            True if notification should be sent, False otherwise
        """
        prefs = device_info.preferences

        # Map notification types to preference keys
        # AC Charging notifications
        if notification_type in ('charge_complete', 'charging_started'):
            enabled = prefs.get('ac_charging_notifications', True)
            if not enabled:
                logger.debug(
                    f"Skipping {notification_type} for {device_info.token[:20]}... "
                    "(AC charging notifications disabled)"
                )
            return enabled

        # EVCC mode change notifications
        if notification_type == 'evcc_mode_changed':
            enabled = prefs.get('evcc_mode_change_notifications', True)
            if not enabled:
                logger.debug(
                    f"Skipping {notification_type} for {device_info.token[:20]}... "
                    "(EVCC mode change notifications disabled)"
                )
            return enabled

        # EVCC battery boost notifications
        if notification_type == 'evcc_battery_boost_activated':
            enabled = prefs.get('evcc_battery_boost_notifications', True)
            if not enabled:
                logger.debug(
                    f"Skipping {notification_type} for {device_info.token[:20]}... "
                    "(EVCC battery boost notifications disabled)"
                )
            return enabled

        # EVCC "Charging Started" notifications (solar, fast, minSolar modes)
        # These are automatic state changes that can be noisy, especially in PV mode
        # OFF by default - separate from Live Activities preference
        if notification_type in (
            'evcc_fast_charging_started',
            'evcc_solar_charging_started',
            'evcc_minsolar_charging_started',
        ):
            enabled = prefs.get('evcc_charging_started_notifications', False)  # OFF by default
            if not enabled:
                logger.debug(
                    f"Skipping {notification_type} for {device_info.token[:20]}... "
                    "(EVCC Charging Started notifications disabled)"
                )
            return enabled

        # EVCC Live Activities (plan and update notifications - NOT charging started)
        if notification_type in (
            'evcc_plan_activated',
            'evcc_plan_charging_started',
            'evcc_plan_charging_update',
            'evcc_plan_complete',
            'evcc_fast_charging_stopped',
            'evcc_solar_charging_stopped',
            'evcc_minsolar_charging_stopped',
            'evcc_charging_update'
        ):
            enabled = prefs.get('evcc_live_activities', True)
            if not enabled:
                logger.debug(
                    f"Skipping {notification_type} for {device_info.token[:20]}... "
                    "(EVCC Live Activities disabled)"
                )
            return enabled

        # Default: allow notification if preference not explicitly checked
        return True

    async def send_charge_complete(self, target_soc: int, final_soc: int) -> int:
        """Send charge complete notification to devices with AC charging notifications enabled.

        Args:
            target_soc: Target SOC that was set
            final_soc: Actual final SOC achieved

        Returns:
            Number of notifications sent successfully
        """
        if not self._initialized or not self.client:
            logger.debug("APNs not initialized, skipping notification")
            return 0

        if not self.devices:
            logger.debug("No device tokens registered, skipping notification")
            return 0

        success_count = 0
        failed_tokens = []

        for device_info in self.devices.values():
            # Check if user wants AC charging notifications
            if not self._should_send_notification(device_info, 'charge_complete'):
                continue

            try:
                request = NotificationRequest(
                    device_token=device_info.token,
                    message={
                        "aps": {
                            "content-available": 1,
                            "alert": {
                                "title": "Charging Complete",
                                "body": f"Battery reached {final_soc}% (target: {target_soc}%)"
                            },
                            "sound": "default"
                        },
                        "type": "charge_complete",
                        "target_soc": target_soc,
                        "final_soc": final_soc,
                    },
                    push_type=PushType.ALERT,
                )
                response = await self.client.send_notification(request)

                if response.is_successful:
                    success_count += 1
                    logger.info(f"Charge complete notification sent to {device_info.token[:20]}...")
                else:
                    logger.warning(
                        f"Failed to send notification to {device_info.token[:20]}...: "
                        f"{response.description}"
                    )
                    # If token is invalid, mark for removal
                    if response.description in ("BadDeviceToken", "Unregistered"):
                        failed_tokens.append(device_info.token)

            except Exception as e:
                logger.error(f"Error sending notification to {device_info.token[:20]}...: {e}")

        # Clean up invalid tokens
        for token in failed_tokens:
            if token in self.devices:
                del self.devices[token]
                logger.info(f"Removed invalid token: {token[:20]}...")

        logger.info(f"Charge complete notifications: {success_count}/{len(self.devices)} successful")
        return success_count

    async def send_charging_started(self, target_soc: int, current_soc: Optional[int] = None) -> int:
        """Send charging started notification to devices with AC charging notifications enabled.

        Args:
            target_soc: Target SOC for this charging session
            current_soc: Current SOC when charging started

        Returns:
            Number of notifications sent successfully
        """
        if not self._initialized or not self.client:
            return 0

        if not self.devices:
            return 0

        success_count = 0

        for device_info in self.devices.values():
            # Check if user wants AC charging notifications
            if not self._should_send_notification(device_info, 'charging_started'):
                continue

            try:
                body = f"Charging to {target_soc}%"
                if current_soc is not None:
                    body = f"Charging from {current_soc}% to {target_soc}%"

                request = NotificationRequest(
                    device_token=device_info.token,
                    message={
                        "aps": {
                            "content-available": 1,
                            "alert": {
                                "title": "Charging Started",
                                "body": body
                            },
                        },
                        "type": "charging_started",
                        "target_soc": target_soc,
                        "current_soc": current_soc,
                    },
                    push_type=PushType.ALERT,
                )
                response = await self.client.send_notification(request)

                if response.is_successful:
                    success_count += 1

            except Exception as e:
                logger.error(f"Error sending charging started notification: {e}")

        return success_count

    # =========================================================================
    # EVCC Notification Methods
    # =========================================================================

    async def send_evcc_mode_changed(
        self,
        previous_mode: str,
        new_mode: str,
        vehicle_soc: int
    ) -> int:
        """Send EVCC mode change notification to devices with mode change notifications enabled."""
        # Map EVCC mode names to user-friendly names
        mode_map = {
            "off": "Off",
            "now": "Fast",
            "pv": "Solar",
            "minpv": "Min+Solar"
        }

        prev_display = mode_map.get(previous_mode, previous_mode)
        new_display = mode_map.get(new_mode, new_mode)

        # Send as silent/background notification - iOS app will create local notification
        # only if user has mode change alerts enabled (fixes notification showing when disabled)
        return await self._send_evcc_notification(
            notification_type="evcc_mode_changed",
            title="Charging Mode Changed",
            body=f"Mode changed from {prev_display} to {new_display}",
            data={
                "previous_mode": previous_mode,
                "new_mode": new_mode,
                "vehicle_soc": vehicle_soc,
                "title": "Charging Mode Changed",
                "body": f"Mode changed from {prev_display} to {new_display}",
            },
            silent=True  # Silent - iOS app creates local notification if enabled
        )

    async def send_evcc_plan_activated(
        self,
        plan_number: int,
        departure_time: str,
        target_soc: int
    ) -> int:
        """Send notification when one-time plan is activated (before charging)."""
        # Defensive: ensure target_soc is never None (use 100% as fallback)
        if target_soc is None:
            logger.warning("target_soc is None in send_evcc_plan_activated, using 100% fallback")
            target_soc = 100

        return await self._send_evcc_notification(
            notification_type="evcc_plan_activated",
            title="Departure Plan Active",
            body=f"Charging to {target_soc}% by {departure_time}",
            data={
                "plan_number": plan_number,
                "departure_time": departure_time,
                "target_soc": target_soc,
            },
            silent=True  # Silent - iOS will show Live Activity
        )

    async def send_evcc_plan_charging_started(
        self,
        plan_number: int,
        departure_time: str,
        target_soc: int,
        charging_power: float,
        mode: str
    ) -> int:
        """Send silent background notification to update Live Activity for plan charging."""
        # Defensive: ensure target_soc is never None (use 100% as fallback)
        if target_soc is None:
            logger.warning("target_soc is None in send_evcc_plan_charging_started, using 100% fallback")
            target_soc = 100

        return await self._send_evcc_notification(
            notification_type="evcc_plan_charging_started",
            title="Departure Plan Charging Started",
            body=f"Charging to {target_soc}% at {charging_power/1000:.1f} kW",
            data={
                "plan_number": plan_number,
                "departure_time": departure_time,
                "target_soc": target_soc,
                "charging_power": int(charging_power),
                "mode": mode,
            },
            silent=True  # Silent - updates Live Activity without visible banner
        )

    async def send_evcc_plan_charging_update(
        self,
        plan_number: int,
        current_soc: int,
        charging_power: float,
        remaining_minutes: int
    ) -> int:
        """Send periodic update while plan is charging."""
        return await self._send_evcc_notification(
            notification_type="evcc_plan_charging_update",
            title=None,  # Silent update
            body=None,
            data={
                "plan_number": plan_number,
                "current_soc": current_soc,
                "charging_power": int(charging_power),
                "remaining_minutes": remaining_minutes,
            },
            silent=True
        )

    async def send_evcc_plan_complete(
        self,
        plan_number: int,
        final_soc: int,
        charged_kwh: float
    ) -> int:
        """Send notification when plan charging finishes."""
        return await self._send_evcc_notification(
            notification_type="evcc_plan_complete",
            title="Charging Complete",
            body=f"Reached {final_soc}% (+{charged_kwh:.1f} kWh)",
            data={
                "plan_number": plan_number,
                "final_soc": final_soc,
                "charged_energy": charged_kwh,
            },
            silent=True  # iOS dismisses Live Activity
        )

    async def send_evcc_fast_charging_started(
        self,
        current_soc: int,
        charging_power: float
    ) -> int:
        """Send alert notification to wake app and trigger Fast charging Live Activity."""
        return await self._send_evcc_notification(
            notification_type="evcc_fast_charging_started",
            title="Started Charging in Fast Mode",
            body=f"Charging at {charging_power/1000:.1f} kW",
            data={
                "current_soc": current_soc,
                "charging_power": int(charging_power),
            },
            silent=False  # Alert - wakes app to start Live Activity
        )

    async def send_evcc_fast_charging_stopped(
        self,
        final_soc: int,
        charged_kwh: float,
        duration_minutes: int
    ) -> int:
        """Send notification when fast mode charging ends."""
        return await self._send_evcc_notification(
            notification_type="evcc_fast_charging_stopped",
            title="Fast Charging Complete",
            body=f"Charged to {final_soc}% (+{charged_kwh:.1f} kWh)",
            data={
                "final_soc": final_soc,
                "charged_energy": charged_kwh,
                "duration_minutes": duration_minutes,
            },
            silent=True
        )

    async def send_evcc_solar_charging_started(
        self,
        current_soc: int,
        solar_power: float,
        charging_power: float
    ) -> int:
        """Send alert notification to wake app and trigger Solar charging Live Activity."""
        return await self._send_evcc_notification(
            notification_type="evcc_solar_charging_started",
            title="Started Charging in Solar Mode",
            body=f"Charging at {charging_power/1000:.1f} kW from solar",
            data={
                "current_soc": current_soc,
                "solar_power": int(solar_power),
                "charging_power": int(charging_power),
            },
            silent=False  # Alert - wakes app to start Live Activity
        )

    async def send_evcc_solar_charging_stopped(
        self,
        final_soc: int,
        charged_kwh: float,
        solar_percentage: float
    ) -> int:
        """Send notification when solar charging ends."""
        return await self._send_evcc_notification(
            notification_type="evcc_solar_charging_stopped",
            title="Solar Charging Complete",
            body=f"Charged to {final_soc}% (+{charged_kwh:.1f} kWh)",
            data={
                "final_soc": final_soc,
                "charged_energy": charged_kwh,
                "solar_percentage": solar_percentage,
            },
            silent=True
        )

    async def send_evcc_minsolar_charging_started(
        self,
        current_soc: int,
        min_power: float,
        solar_power: float,
        charging_power: float
    ) -> int:
        """Send alert notification to wake app and trigger Min+Solar charging Live Activity."""
        return await self._send_evcc_notification(
            notification_type="evcc_minsolar_charging_started",
            title="Started Charging in Min+Solar Mode",
            body=f"Charging at {charging_power/1000:.1f} kW",
            data={
                "current_soc": current_soc,
                "min_power": int(min_power),
                "solar_power": int(solar_power),
                "charging_power": int(charging_power),
            },
            silent=False  # Alert - wakes app to start Live Activity
        )

    async def send_evcc_minsolar_charging_stopped(
        self,
        final_soc: int,
        charged_kwh: float
    ) -> int:
        """Send notification when min+solar charging ends."""
        return await self._send_evcc_notification(
            notification_type="evcc_minsolar_charging_stopped",
            title="Min+Solar Charging Complete",
            body=f"Charged to {final_soc}% (+{charged_kwh:.1f} kWh)",
            data={
                "final_soc": final_soc,
                "charged_energy": charged_kwh,
            },
            silent=True
        )

    async def send_evcc_battery_boost_activated(
        self,
        vehicle_soc: int,
        battery_power: float,
        home_soc: int
    ) -> int:
        """Send notification when home battery starts boosting EV."""
        return await self._send_evcc_notification(
            notification_type="evcc_battery_boost_activated",
            title="Battery Boost Active",
            body=f"Home battery ({home_soc}%) boosting EV at {battery_power/1000:.1f} kW",
            data={
                "vehicle_soc": vehicle_soc,
                "battery_power": int(battery_power),
                "home_soc": home_soc,
            },
            silent=False  # User-facing notification
        )

    async def send_evcc_charging_update(
        self,
        current_soc: int,
        charging_power: float,
        mode: str,
        solar_power: Optional[float] = None
    ) -> int:
        """Send periodic charging update (any mode)."""
        return await self._send_evcc_notification(
            notification_type="evcc_charging_update",
            title=None,
            body=None,
            data={
                "current_soc": current_soc,
                "charging_power": int(charging_power),
                "mode": mode,
                "solar_power": int(solar_power) if solar_power else None,
            },
            silent=True
        )

    async def _send_evcc_notification(
        self,
        notification_type: str,
        title: Optional[str],
        body: Optional[str],
        data: dict,
        silent: bool = True
    ) -> int:
        """Internal helper to send EVCC notifications with preference filtering.

        Checks each device's notification preferences before sending.
        Only sends to devices that have enabled the specific notification type.

        Args:
            notification_type: Type identifier for the notification
            title: Alert title (None for silent)
            body: Alert body (None for silent)
            data: Custom data payload
            silent: If True, send as content-available only

        Returns:
            Number of notifications sent successfully
        """
        if not self._initialized or not self.client:
            logger.debug("APNs not initialized, skipping EVCC notification")
            return 0

        if not self.devices:
            logger.debug("No device tokens registered, skipping EVCC notification")
            return 0

        success_count = 0
        failed_tokens = []

        for device_info in self.devices.values():
            # Check if user wants this type of notification
            if not self._should_send_notification(device_info, notification_type):
                continue

            try:
                # Build APS payload
                aps: dict = {"content-available": 1}
                if not silent and title and body:
                    aps["alert"] = {"title": title, "body": body}
                    aps["sound"] = "default"

                message = {
                    "aps": aps,
                    "type": notification_type,
                    **data
                }

                push_type = PushType.BACKGROUND if silent else PushType.ALERT

                # Debug: Log the full notification payload
                logger.info(f"📤 Sending {notification_type} notification:")
                logger.info(f"   Push Type: {push_type}")
                logger.info(f"   Silent: {silent}")
                logger.info(f"   Payload: {message}")

                request = NotificationRequest(
                    device_token=device_info.token,
                    message=message,
                    push_type=push_type,
                )
                response = await self.client.send_notification(request)

                # Debug: Log the response
                logger.info(f"   APNs Response: is_successful={response.is_successful}, description={response.description}")

                if response.is_successful:
                    success_count += 1
                    logger.debug(
                        f"EVCC notification ({notification_type}) sent to {device_info.token[:20]}..."
                    )
                else:
                    logger.warning(
                        f"Failed EVCC notification to {device_info.token[:20]}...: "
                        f"{response.description}"
                    )
                    if response.description in ("BadDeviceToken", "Unregistered"):
                        failed_tokens.append(device_info.token)

            except Exception as e:
                logger.error(f"Error sending EVCC notification ({notification_type}): {e}")

        # Clean up invalid tokens
        for token in failed_tokens:
            if token in self.devices:
                del self.devices[token]

        if success_count > 0:
            logger.info(f"EVCC notification ({notification_type}): {success_count} sent")

        return success_count

    @property
    def is_enabled(self) -> bool:
        """Check if APNs service is enabled and initialized."""
        return self._initialized and self.client is not None

    @property
    def registered_device_count(self) -> int:
        """Get number of registered devices."""
        return len(self.devices)
