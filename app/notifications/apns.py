"""Apple Push Notification service for iOS app notifications."""

import logging
from pathlib import Path
from typing import Optional, Set

from aioapns import APNs, NotificationRequest, PushType

from ..models import APNsConfig

logger = logging.getLogger(__name__)


class APNsService:
    """Service for sending push notifications to iOS devices."""

    def __init__(self, config: APNsConfig):
        """Initialize APNs service with configuration.

        Args:
            config: APNs configuration with key path, key ID, team ID, and bundle ID
        """
        self.config = config
        self.client: Optional[APNs] = None
        self.device_tokens: Set[str] = set()
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

    def register_device(self, device_token: str) -> bool:
        """Register a device token for push notifications.

        Args:
            device_token: Hex-encoded device token from iOS

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

        self.device_tokens.add(device_token)
        logger.info(f"Registered device token: {device_token[:20]}... (total: {len(self.device_tokens)})")
        return True

    def unregister_device(self, device_token: str) -> bool:
        """Unregister a device token.

        Args:
            device_token: Device token to remove

        Returns:
            True if token was found and removed
        """
        if device_token in self.device_tokens:
            self.device_tokens.discard(device_token)
            logger.info(f"Unregistered device token: {device_token[:20]}...")
            return True
        return False

    async def send_charge_complete(self, target_soc: int, final_soc: int) -> int:
        """Send charge complete notification to all registered devices.

        Args:
            target_soc: Target SOC that was set
            final_soc: Actual final SOC achieved

        Returns:
            Number of notifications sent successfully
        """
        if not self._initialized or not self.client:
            logger.debug("APNs not initialized, skipping notification")
            return 0

        if not self.device_tokens:
            logger.debug("No device tokens registered, skipping notification")
            return 0

        success_count = 0
        failed_tokens = []

        for token in self.device_tokens:
            try:
                request = NotificationRequest(
                    device_token=token,
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
                    logger.info(f"Charge complete notification sent to {token[:20]}...")
                else:
                    logger.warning(f"Failed to send notification to {token[:20]}...: {response.description}")
                    # If token is invalid, mark for removal
                    if response.description in ("BadDeviceToken", "Unregistered"):
                        failed_tokens.append(token)

            except Exception as e:
                logger.error(f"Error sending notification to {token[:20]}...: {e}")

        # Clean up invalid tokens
        for token in failed_tokens:
            self.device_tokens.discard(token)
            logger.info(f"Removed invalid token: {token[:20]}...")

        logger.info(f"Charge complete notifications: {success_count}/{len(self.device_tokens)} successful")
        return success_count

    async def send_charging_started(self, target_soc: int, current_soc: Optional[int] = None) -> int:
        """Send charging started notification to all registered devices.

        Args:
            target_soc: Target SOC for this charging session
            current_soc: Current SOC when charging started

        Returns:
            Number of notifications sent successfully
        """
        if not self._initialized or not self.client:
            return 0

        if not self.device_tokens:
            return 0

        success_count = 0

        for token in self.device_tokens:
            try:
                body = f"Charging to {target_soc}%"
                if current_soc is not None:
                    body = f"Charging from {current_soc}% to {target_soc}%"

                request = NotificationRequest(
                    device_token=token,
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
        """Send EVCC mode change notification."""
        return await self._send_evcc_notification(
            notification_type="evcc_mode_changed",
            title="Charging Mode Changed",
            body=f"Mode changed from {previous_mode} to {new_mode}",
            data={
                "previous_mode": previous_mode,
                "new_mode": new_mode,
                "vehicle_soc": vehicle_soc,
            }
        )

    async def send_evcc_plan_activated(
        self,
        plan_number: int,
        departure_time: str,
        target_soc: int
    ) -> int:
        """Send notification when one-time plan is activated (before charging)."""
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
        """Send notification when plan-based charging begins."""
        return await self._send_evcc_notification(
            notification_type="evcc_plan_charging_started",
            title="Plan Charging Started",
            body=f"Charging to {target_soc}% at {charging_power/1000:.1f} kW",
            data={
                "plan_number": plan_number,
                "departure_time": departure_time,
                "target_soc": target_soc,
                "charging_power": int(charging_power),
                "mode": mode,
            },
            silent=True
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
        """Send notification when fast mode charging begins."""
        return await self._send_evcc_notification(
            notification_type="evcc_fast_charging_started",
            title="Fast Charging Started",
            body=f"Charging at {charging_power/1000:.1f} kW",
            data={
                "current_soc": current_soc,
                "charging_power": int(charging_power),
            },
            silent=True
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
        """Send notification when solar charging begins."""
        return await self._send_evcc_notification(
            notification_type="evcc_solar_charging_started",
            title="Solar Charging Started",
            body=f"Charging at {charging_power/1000:.1f} kW from solar",
            data={
                "current_soc": current_soc,
                "solar_power": int(solar_power),
                "charging_power": int(charging_power),
            },
            silent=True
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
        """Send notification when min+solar charging begins."""
        return await self._send_evcc_notification(
            notification_type="evcc_minsolar_charging_started",
            title="Min+Solar Charging Started",
            body=f"Charging at {charging_power/1000:.1f} kW",
            data={
                "current_soc": current_soc,
                "min_power": int(min_power),
                "solar_power": int(solar_power),
                "charging_power": int(charging_power),
            },
            silent=True
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
            silent=False  # This one should alert the user
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
        """Internal helper to send EVCC notifications.

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

        if not self.device_tokens:
            logger.debug("No device tokens registered, skipping EVCC notification")
            return 0

        success_count = 0
        failed_tokens = []

        for token in self.device_tokens:
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

                request = NotificationRequest(
                    device_token=token,
                    message=message,
                    push_type=push_type,
                )
                response = await self.client.send_notification(request)

                if response.is_successful:
                    success_count += 1
                    logger.debug(f"EVCC notification ({notification_type}) sent to {token[:20]}...")
                else:
                    logger.warning(f"Failed EVCC notification to {token[:20]}...: {response.description}")
                    if response.description in ("BadDeviceToken", "Unregistered"):
                        failed_tokens.append(token)

            except Exception as e:
                logger.error(f"Error sending EVCC notification ({notification_type}): {e}")

        # Clean up invalid tokens
        for token in failed_tokens:
            self.device_tokens.discard(token)

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
        return len(self.device_tokens)
