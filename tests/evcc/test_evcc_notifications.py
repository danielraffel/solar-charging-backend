"""Tests for EVCC APNs notification methods."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.notifications.apns import APNsService
from app.models import APNsConfig


class TestEVCCNotifications:
    """Tests for EVCC-related APNs notification methods."""

    @pytest.fixture
    def apns_config(self):
        """Create APNs config for testing."""
        return APNsConfig(
            enabled=True,
            key_path="/path/to/key.p8",
            key_id="KEYID12345",
            team_id="TEAMID1234",
            bundle_id="com.example.app",
            use_sandbox=True
        )

    @pytest.fixture
    def apns_service(self, apns_config):
        """Create APNs service with mocked client."""
        service = APNsService(apns_config)
        service._initialized = True
        service.device_tokens = {"test_token_abc123"}

        # Mock the client
        mock_response = MagicMock()
        mock_response.is_successful = True

        mock_client = MagicMock()
        mock_client.send_notification = AsyncMock(return_value=mock_response)
        service.client = mock_client

        return service

    # =========================================================================
    # Mode Change Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_mode_changed(self, apns_service):
        """Test mode change notification."""
        result = await apns_service.send_evcc_mode_changed(
            previous_mode="off",
            new_mode="now",
            vehicle_soc=50
        )

        assert result == 1
        apns_service.client.send_notification.assert_called_once()

        # Verify notification payload
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_mode_changed"
        assert request.message["previous_mode"] == "off"
        assert request.message["new_mode"] == "now"
        assert request.message["vehicle_soc"] == 50

    # =========================================================================
    # Departure Plan Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_plan_activated(self, apns_service):
        """Test plan activated notification."""
        result = await apns_service.send_evcc_plan_activated(
            plan_number=1,
            departure_time="08:00",
            target_soc=80
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_plan_activated"
        assert request.message["plan_number"] == 1
        assert request.message["departure_time"] == "08:00"
        assert request.message["target_soc"] == 80

    @pytest.mark.asyncio
    async def test_send_evcc_plan_charging_started(self, apns_service):
        """Test plan charging started notification."""
        result = await apns_service.send_evcc_plan_charging_started(
            plan_number=1,
            departure_time="08:00",
            target_soc=80,
            charging_power=7400.0,
            mode="now"
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_plan_charging_started"
        assert request.message["charging_power"] == 7400

    @pytest.mark.asyncio
    async def test_send_evcc_plan_charging_update(self, apns_service):
        """Test plan charging update notification."""
        result = await apns_service.send_evcc_plan_charging_update(
            plan_number=1,
            current_soc=65,
            charging_power=7400.0,
            remaining_minutes=45
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_plan_charging_update"
        assert request.message["current_soc"] == 65
        assert request.message["remaining_minutes"] == 45

    @pytest.mark.asyncio
    async def test_send_evcc_plan_complete(self, apns_service):
        """Test plan complete notification."""
        result = await apns_service.send_evcc_plan_complete(
            plan_number=1,
            final_soc=80,
            charged_kwh=25.5
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_plan_complete"
        assert request.message["final_soc"] == 80
        assert request.message["charged_energy"] == 25.5

    # =========================================================================
    # Fast/Now Mode Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_fast_charging_started(self, apns_service):
        """Test fast charging started notification."""
        result = await apns_service.send_evcc_fast_charging_started(
            current_soc=50,
            charging_power=7400.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_fast_charging_started"
        assert request.message["current_soc"] == 50
        assert request.message["charging_power"] == 7400

    @pytest.mark.asyncio
    async def test_send_evcc_fast_charging_stopped(self, apns_service):
        """Test fast charging stopped notification."""
        result = await apns_service.send_evcc_fast_charging_stopped(
            final_soc=80,
            charged_kwh=25.5,
            duration_minutes=120
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_fast_charging_stopped"
        assert request.message["final_soc"] == 80
        assert request.message["charged_energy"] == 25.5
        assert request.message["duration_minutes"] == 120

    # =========================================================================
    # Solar/PV Mode Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_solar_charging_started(self, apns_service):
        """Test solar charging started notification."""
        result = await apns_service.send_evcc_solar_charging_started(
            current_soc=50,
            solar_power=5000.0,
            charging_power=4500.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_solar_charging_started"
        assert request.message["solar_power"] == 5000
        assert request.message["charging_power"] == 4500

    @pytest.mark.asyncio
    async def test_send_evcc_solar_charging_stopped(self, apns_service):
        """Test solar charging stopped notification."""
        result = await apns_service.send_evcc_solar_charging_stopped(
            final_soc=70,
            charged_kwh=15.0,
            solar_percentage=85.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_solar_charging_stopped"
        assert request.message["solar_percentage"] == 85.0

    # =========================================================================
    # Min+Solar Mode Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_minsolar_charging_started(self, apns_service):
        """Test min+solar charging started notification."""
        result = await apns_service.send_evcc_minsolar_charging_started(
            current_soc=50,
            min_power=1400.0,
            solar_power=3500.0,
            charging_power=4900.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_minsolar_charging_started"
        assert request.message["min_power"] == 1400
        assert request.message["solar_power"] == 3500

    @pytest.mark.asyncio
    async def test_send_evcc_minsolar_charging_stopped(self, apns_service):
        """Test min+solar charging stopped notification."""
        result = await apns_service.send_evcc_minsolar_charging_stopped(
            final_soc=75,
            charged_kwh=20.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_minsolar_charging_stopped"
        assert request.message["final_soc"] == 75

    # =========================================================================
    # Battery Boost Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_battery_boost_activated(self, apns_service):
        """Test battery boost notification."""
        result = await apns_service.send_evcc_battery_boost_activated(
            vehicle_soc=50,
            battery_power=3000.0,
            home_soc=80
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_battery_boost_activated"
        assert request.message["vehicle_soc"] == 50
        assert request.message["battery_power"] == 3000
        assert request.message["home_soc"] == 80
        # Battery boost should NOT be silent
        assert "alert" in request.message["aps"]

    # =========================================================================
    # Periodic Update Notification Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_send_evcc_charging_update(self, apns_service):
        """Test periodic charging update notification."""
        result = await apns_service.send_evcc_charging_update(
            current_soc=65,
            charging_power=7400.0,
            mode="now",
            solar_power=0.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["type"] == "evcc_charging_update"
        assert request.message["current_soc"] == 65
        assert request.message["mode"] == "now"

    @pytest.mark.asyncio
    async def test_send_evcc_charging_update_with_solar(self, apns_service):
        """Test periodic charging update with solar power."""
        result = await apns_service.send_evcc_charging_update(
            current_soc=65,
            charging_power=4500.0,
            mode="pv",
            solar_power=5000.0
        )

        assert result == 1
        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]
        assert request.message["solar_power"] == 5000

    # =========================================================================
    # Error Handling Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_notification_not_initialized(self, apns_config):
        """Test notifications return 0 when service not initialized."""
        service = APNsService(apns_config)
        # Not initialized - _initialized is False

        result = await service.send_evcc_mode_changed(
            previous_mode="off",
            new_mode="now",
            vehicle_soc=50
        )

        assert result == 0

    @pytest.mark.asyncio
    async def test_notification_no_device_tokens(self, apns_service):
        """Test notifications return 0 when no device tokens registered."""
        apns_service.device_tokens = set()  # Empty

        result = await apns_service.send_evcc_mode_changed(
            previous_mode="off",
            new_mode="now",
            vehicle_soc=50
        )

        assert result == 0

    @pytest.mark.asyncio
    async def test_notification_failed_token_removed(self, apns_service):
        """Test that failed tokens are removed from device_tokens."""
        # Add a single token that will fail with BadDeviceToken
        apns_service.device_tokens = {"bad_token"}

        mock_failure = MagicMock()
        mock_failure.is_successful = False
        mock_failure.description = "BadDeviceToken"

        apns_service.client.send_notification = AsyncMock(
            return_value=mock_failure
        )

        result = await apns_service.send_evcc_mode_changed(
            previous_mode="off",
            new_mode="now",
            vehicle_soc=50
        )

        # Zero successes, bad token should be removed
        assert result == 0
        assert len(apns_service.device_tokens) == 0

    @pytest.mark.asyncio
    async def test_notification_handles_exception(self, apns_service):
        """Test that exceptions don't crash the notification send."""
        apns_service.client.send_notification = AsyncMock(
            side_effect=Exception("Network error")
        )

        # Should not raise, should return 0
        result = await apns_service.send_evcc_mode_changed(
            previous_mode="off",
            new_mode="now",
            vehicle_soc=50
        )

        assert result == 0


class TestEVCCNotificationPayloads:
    """Tests for EVCC notification payload structure."""

    @pytest.fixture
    def apns_service(self):
        """Create APNs service with mocked client."""
        config = APNsConfig(
            enabled=True,
            key_path="/path/to/key.p8",
            key_id="KEYID12345",
            team_id="TEAMID1234",
            bundle_id="com.example.app",
            use_sandbox=True
        )
        service = APNsService(config)
        service._initialized = True
        service.device_tokens = {"test_token"}

        mock_response = MagicMock()
        mock_response.is_successful = True
        mock_client = MagicMock()
        mock_client.send_notification = AsyncMock(return_value=mock_response)
        service.client = mock_client

        return service

    @pytest.mark.asyncio
    async def test_silent_notification_structure(self, apns_service):
        """Test silent notifications have correct structure."""
        await apns_service.send_evcc_plan_activated(
            plan_number=1,
            departure_time="08:00",
            target_soc=80
        )

        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]

        # Silent notifications should have content-available
        assert request.message["aps"]["content-available"] == 1
        # Should NOT have alert
        assert "alert" not in request.message["aps"]

    @pytest.mark.asyncio
    async def test_alert_notification_structure(self, apns_service):
        """Test alert notifications have correct structure."""
        await apns_service.send_evcc_battery_boost_activated(
            vehicle_soc=50,
            battery_power=3000.0,
            home_soc=80
        )

        call_args = apns_service.client.send_notification.call_args
        request = call_args[0][0]

        # Alert notifications should have alert and sound
        assert "alert" in request.message["aps"]
        assert request.message["aps"]["alert"]["title"] == "Battery Boost Active"
        assert "sound" in request.message["aps"]
