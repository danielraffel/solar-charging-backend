"""Tests for APNs notification service."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture
def apns_config():
    """Create a test APNs configuration."""
    from app.models import APNsConfig
    return APNsConfig(
        enabled=True,
        key_path="/tmp/test_key.p8",
        key_id="TESTKEY123",
        team_id="TESTTEAM01",
        bundle_id="com.test.app",
        use_sandbox=True
    )


@pytest.fixture
def disabled_apns_config():
    """Create a disabled APNs configuration."""
    from app.models import APNsConfig
    return APNsConfig(
        enabled=False,
        key_path="",
        key_id="",
        team_id="",
        bundle_id="",
        use_sandbox=False
    )


class TestAPNsServiceInit:
    """Tests for APNs service initialization."""

    def test_service_disabled(self, disabled_apns_config):
        """Test service when APNs is disabled in config."""
        from app.notifications import APNsService

        service = APNsService(disabled_apns_config)

        assert service.is_enabled is False
        assert service.registered_device_count == 0

    def test_service_enabled_no_key_file(self, apns_config):
        """Test service when key file doesn't exist."""
        from app.notifications import APNsService

        service = APNsService(apns_config)

        # Before initialization, service should not be enabled
        assert service.is_enabled is False

    @pytest.mark.asyncio
    async def test_initialize_missing_key_file(self, apns_config):
        """Test initialization fails with missing key file."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        result = await service.initialize()

        assert result is False
        assert service.is_enabled is False


class TestDeviceTokenManagement:
    """Tests for device token registration and management."""

    def test_register_valid_token(self, apns_config):
        """Test registering a valid device token."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        token = "a" * 64  # 64 character hex token

        result = service.register_device(token)

        assert result is True
        assert service.registered_device_count == 1
        assert token in service.device_tokens

    def test_register_empty_token(self, apns_config):
        """Test registering an empty token fails."""
        from app.notifications import APNsService

        service = APNsService(apns_config)

        result = service.register_device("")

        assert result is False
        assert service.registered_device_count == 0

    def test_register_invalid_hex_token(self, apns_config):
        """Test registering a non-hex token fails."""
        from app.notifications import APNsService

        service = APNsService(apns_config)

        result = service.register_device("invalid-token-xyz!")

        assert result is False
        assert service.registered_device_count == 0

    def test_register_duplicate_token(self, apns_config):
        """Test registering the same token twice."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        token = "b" * 64

        service.register_device(token)
        result = service.register_device(token)

        assert result is True
        assert service.registered_device_count == 1  # Still just one

    def test_unregister_existing_token(self, apns_config):
        """Test unregistering an existing token."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        token = "c" * 64
        service.register_device(token)

        result = service.unregister_device(token)

        assert result is True
        assert service.registered_device_count == 0

    def test_unregister_nonexistent_token(self, apns_config):
        """Test unregistering a token that doesn't exist."""
        from app.notifications import APNsService

        service = APNsService(apns_config)

        result = service.unregister_device("nonexistent" * 8)

        assert result is False

    def test_multiple_tokens(self, apns_config):
        """Test managing multiple device tokens."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        tokens = ["a" * 64, "b" * 64, "c" * 64]

        for token in tokens:
            service.register_device(token)

        assert service.registered_device_count == 3

        service.unregister_device(tokens[1])
        assert service.registered_device_count == 2


class TestNotificationSending:
    """Tests for sending push notifications."""

    @pytest.mark.asyncio
    async def test_send_without_initialization(self, apns_config):
        """Test sending notification without initialization returns 0."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        service.register_device("d" * 64)

        result = await service.send_charge_complete(target_soc=80, final_soc=82)

        assert result == 0  # No notifications sent because not initialized

    @pytest.mark.asyncio
    async def test_send_no_registered_devices(self, apns_config):
        """Test sending notification with no registered devices."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        service._initialized = True
        service.client = MagicMock()

        result = await service.send_charge_complete(target_soc=80, final_soc=82)

        assert result == 0

    @pytest.mark.asyncio
    async def test_send_charge_complete_success(self, apns_config):
        """Test successful charge complete notification."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        service._initialized = True

        # Mock the APNs client
        mock_response = MagicMock()
        mock_response.is_successful = True
        mock_client = MagicMock()
        mock_client.send_notification = AsyncMock(return_value=mock_response)
        service.client = mock_client

        # Register devices
        service.register_device("e" * 64)
        service.register_device("f" * 64)

        result = await service.send_charge_complete(target_soc=85, final_soc=87)

        assert result == 2
        assert mock_client.send_notification.call_count == 2

    @pytest.mark.asyncio
    async def test_send_charge_complete_partial_failure(self, apns_config):
        """Test charge complete with some failed deliveries."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        service._initialized = True

        # Mock responses - one success, one failure
        success_response = MagicMock()
        success_response.is_successful = True

        failure_response = MagicMock()
        failure_response.is_successful = False
        failure_response.description = "BadDeviceToken"

        mock_client = MagicMock()
        mock_client.send_notification = AsyncMock(
            side_effect=[success_response, failure_response]
        )
        service.client = mock_client

        # Register devices
        service.register_device("g" * 64)
        service.register_device("h" * 64)

        result = await service.send_charge_complete(target_soc=90, final_soc=90)

        assert result == 1  # Only one success
        assert service.registered_device_count == 1  # Bad token was removed

    @pytest.mark.asyncio
    async def test_send_charging_started(self, apns_config):
        """Test charging started notification."""
        from app.notifications import APNsService

        service = APNsService(apns_config)
        service._initialized = True

        mock_response = MagicMock()
        mock_response.is_successful = True
        mock_client = MagicMock()
        mock_client.send_notification = AsyncMock(return_value=mock_response)
        service.client = mock_client

        service.register_device("i" * 64)

        result = await service.send_charging_started(target_soc=80, current_soc=45)

        assert result == 1
        mock_client.send_notification.assert_called_once()


class TestAPNsConfigModel:
    """Tests for APNsConfig model."""

    def test_config_defaults(self):
        """Test APNsConfig default values."""
        from app.models import APNsConfig

        config = APNsConfig()

        assert config.enabled is False
        assert config.key_path == ""
        assert config.key_id == ""
        assert config.team_id == ""
        assert config.bundle_id == ""
        assert config.use_sandbox is False

    def test_config_with_values(self):
        """Test APNsConfig with provided values."""
        from app.models import APNsConfig

        config = APNsConfig(
            enabled=True,
            key_path="/path/to/key.p8",
            key_id="ABC123",
            team_id="XYZ789",
            bundle_id="com.example.app",
            use_sandbox=True
        )

        assert config.enabled is True
        assert config.key_path == "/path/to/key.p8"
        assert config.key_id == "ABC123"
        assert config.team_id == "XYZ789"
        assert config.bundle_id == "com.example.app"
        assert config.use_sandbox is True
