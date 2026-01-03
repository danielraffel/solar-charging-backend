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

    @property
    def is_enabled(self) -> bool:
        """Check if APNs service is enabled and initialized."""
        return self._initialized and self.client is not None

    @property
    def registered_device_count(self) -> int:
        """Get number of registered devices."""
        return len(self.device_tokens)
