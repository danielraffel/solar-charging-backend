"""Tests for MQTT publish/response protocol implementation.

These tests verify that the MQTT client properly waits for /response
confirmations before sending the next setting, as required by the dongle protocol.
"""

import pytest
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock
import json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.mqtt.client import MQTTClient, RESPONSE_TIMEOUT_SECONDS, PUBLISH_DELAY_FALLBACK
from app.models import MQTTConfig


class TestResponseWaitMechanism:
    """Tests for the response wait mechanism."""

    @pytest.fixture
    def mqtt_config(self):
        """Create test MQTT config."""
        return MQTTConfig(
            host="test.broker.com",
            port=1883,
            username="test_user",
            password="test_pass",
            dongle_prefix="dongle-XX:XX:XX:XX:XX:XX"
        )

    @pytest.fixture
    def mqtt_client(self, mqtt_config):
        """Create MQTT client for testing."""
        client = MQTTClient(mqtt_config)
        client.connected = True
        client.client = MagicMock()
        client.client.publish = MagicMock(return_value=MagicMock(rc=0))  # Success
        return client

    def test_publish_and_wait_success(self, mqtt_client):
        """Test _publish_and_wait succeeds when response received."""
        # Simulate response arriving in a separate thread
        def send_response():
            time.sleep(0.1)  # Small delay
            mqtt_client._last_response = {"success": True, "setting": "ACCharge"}
            if mqtt_client._response_event:
                mqtt_client._response_event.set()

        response_thread = threading.Thread(target=send_response)
        response_thread.start()

        result = mqtt_client._publish_and_wait("ACCharge", "1", timeout=5.0)

        response_thread.join()
        assert result is True

    def test_publish_and_wait_timeout(self, mqtt_client):
        """Test _publish_and_wait returns False on timeout."""
        # Don't send any response - let it timeout
        result = mqtt_client._publish_and_wait("ACCharge", "1", timeout=0.5)

        assert result is False

    def test_publish_and_wait_wrong_setting_response(self, mqtt_client):
        """Test that response for wrong setting doesn't trigger success."""
        def send_wrong_response():
            time.sleep(0.1)
            # Response for different setting
            mqtt_client._last_response = {"success": True, "setting": "ACChgMode"}
            # Event not set because setting doesn't match

        response_thread = threading.Thread(target=send_wrong_response)
        response_thread.start()

        result = mqtt_client._publish_and_wait("ACCharge", "1", timeout=0.5)

        response_thread.join()
        assert result is False  # Should timeout, wrong setting

    def test_publish_and_wait_failure_response(self, mqtt_client):
        """Test that success=False response returns False."""
        def send_failure_response():
            time.sleep(0.1)
            mqtt_client._last_response = {"success": False, "setting": "ACCharge"}
            if mqtt_client._response_event:
                mqtt_client._response_event.set()

        response_thread = threading.Thread(target=send_failure_response)
        response_thread.start()

        result = mqtt_client._publish_and_wait("ACCharge", "1", timeout=5.0)

        response_thread.join()
        assert result is False

    def test_publish_not_connected(self, mqtt_client):
        """Test publish fails when not connected."""
        mqtt_client.connected = False

        result = mqtt_client._publish_and_wait("ACCharge", "1", timeout=1.0)

        assert result is False


class TestSequentialPublishing:
    """Tests for sequential settings publishing."""

    @pytest.fixture
    def mqtt_config(self):
        return MQTTConfig(
            host="test.broker.com",
            port=1883,
            username="test_user",
            password="test_pass",
            dongle_prefix="dongle-XX:XX:XX:XX:XX:XX"
        )

    @pytest.fixture
    def mqtt_client(self, mqtt_config):
        client = MQTTClient(mqtt_config)
        client.connected = True
        client.client = MagicMock()
        client.client.publish = MagicMock(return_value=MagicMock(rc=0))
        return client

    def test_sequential_publish_all_success(self, mqtt_client):
        """Test publish_settings_sequentially succeeds when all responses received."""
        settings = [
            ("ACChgStart", "06:00"),
            ("ACChgEnd", "14:00"),
        ]

        # Mock _publish_and_wait to always succeed
        mqtt_client._publish_and_wait = MagicMock(return_value=True)

        result = mqtt_client.publish_settings_sequentially(settings)

        assert result is True
        assert mqtt_client._publish_and_wait.call_count == 2

    def test_sequential_publish_partial_failure(self, mqtt_client):
        """Test publish_settings_sequentially continues on partial failure."""
        settings = [
            ("ACChgStart", "06:00"),
            ("ACChgEnd", "14:00"),
            ("ACChgStart1", "00:00"),
        ]

        # First succeeds, second fails, third succeeds
        mqtt_client._publish_and_wait = MagicMock(side_effect=[True, False, True])

        result = mqtt_client.publish_settings_sequentially(settings)

        assert result is False  # Overall failure due to one failure
        assert mqtt_client._publish_and_wait.call_count == 3  # Still tried all

    def test_sequential_publish_empty_list(self, mqtt_client):
        """Test empty settings list returns True."""
        result = mqtt_client.publish_settings_sequentially([])

        assert result is True


class TestTimeSettingsPublishing:
    """Tests for publish_time_settings method."""

    @pytest.fixture
    def mqtt_config(self):
        return MQTTConfig(
            host="test.broker.com",
            port=1883,
            username="test_user",
            password="test_pass",
            dongle_prefix="dongle-XX:XX:XX:XX:XX:XX"
        )

    @pytest.fixture
    def mqtt_client(self, mqtt_config):
        client = MQTTClient(mqtt_config)
        client.connected = True
        client.client = MagicMock()
        client.client.publish = MagicMock(return_value=MagicMock(rc=0))
        return client

    def test_time_settings_uses_sequential_publish(self, mqtt_client):
        """Test that publish_time_settings uses sequential publishing."""
        mqtt_client.publish_settings_sequentially = MagicMock(return_value=True)

        result = mqtt_client.publish_time_settings("06:00", "14:00")

        assert result is True
        mqtt_client.publish_settings_sequentially.assert_called_once()

        # Verify settings include all time periods
        settings = mqtt_client.publish_settings_sequentially.call_args[0][0]
        assert len(settings) == 6
        assert ("ACChgStart", "06:00") in settings
        assert ("ACChgEnd", "14:00") in settings
        assert ("ACChgStart1", "00:00") in settings

    def test_time_settings_disables_other_periods(self, mqtt_client):
        """Test that other charging periods are set to 00:00."""
        mqtt_client.publish_settings_sequentially = MagicMock(return_value=True)

        mqtt_client.publish_time_settings("08:00", "16:00")

        settings = mqtt_client.publish_settings_sequentially.call_args[0][0]
        settings_dict = dict(settings)

        assert settings_dict["ACChgStart1"] == "00:00"
        assert settings_dict["ACChgEnd1"] == "00:00"
        assert settings_dict["ACChgStart2"] == "00:00"
        assert settings_dict["ACChgEnd2"] == "00:00"


class TestACChargingMethods:
    """Tests for AC charging enable/disable methods."""

    @pytest.fixture
    def mqtt_config(self):
        return MQTTConfig(
            host="test.broker.com",
            port=1883,
            username="test_user",
            password="test_pass",
            dongle_prefix="dongle-XX:XX:XX:XX:XX:XX"
        )

    @pytest.fixture
    def mqtt_client(self, mqtt_config):
        client = MQTTClient(mqtt_config)
        client.connected = True
        client.client = MagicMock()
        client.client.publish = MagicMock(return_value=MagicMock(rc=0))
        return client

    def test_enable_uses_publish_and_wait(self, mqtt_client):
        """Test publish_ac_charge_enable uses response wait."""
        mqtt_client._publish_and_wait = MagicMock(return_value=True)

        result = mqtt_client.publish_ac_charge_enable()

        assert result is True
        mqtt_client._publish_and_wait.assert_called_once_with("ACCharge", "1")

    def test_disable_uses_publish_and_wait(self, mqtt_client):
        """Test publish_ac_charge_disable uses response wait."""
        mqtt_client._publish_and_wait = MagicMock(return_value=True)

        result = mqtt_client.publish_ac_charge_disable()

        assert result is True
        mqtt_client._publish_and_wait.assert_called_once_with("ACCharge", "0")

    def test_soc_limit_uses_publish_and_wait(self, mqtt_client):
        """Test publish_soc_limit uses response wait."""
        mqtt_client._publish_and_wait = MagicMock(return_value=True)

        result = mqtt_client.publish_soc_limit(80)

        assert result is True
        mqtt_client._publish_and_wait.assert_called_once_with("ACChgSOCLimit", "80")

    def test_ac_charge_mode_uses_publish_and_wait(self, mqtt_client):
        """Test publish_ac_charge_mode uses response wait."""
        mqtt_client._publish_and_wait = MagicMock(return_value=True)

        result = mqtt_client.publish_ac_charge_mode(4)

        assert result is True
        mqtt_client._publish_and_wait.assert_called_once_with("ACChgMode", "4")


class TestProtocolConstants:
    """Tests for protocol constants."""

    def test_response_timeout_is_15_seconds(self):
        """Test that default response timeout is 15 seconds."""
        assert RESPONSE_TIMEOUT_SECONDS == 15.0

    def test_fallback_delay_is_half_second(self):
        """Test that fallback delay is 0.5 seconds."""
        assert PUBLISH_DELAY_FALLBACK == 0.5


class TestMessageCallback:
    """Tests for MQTT message callback handling."""

    @pytest.fixture
    def mqtt_config(self):
        return MQTTConfig(
            host="test.broker.com",
            port=1883,
            username="test_user",
            password="test_pass",
            dongle_prefix="dongle-XX:XX:XX:XX:XX:XX"
        )

    @pytest.fixture
    def mqtt_client(self, mqtt_config):
        client = MQTTClient(mqtt_config)
        client.connected = True
        return client

    def test_response_message_sets_event(self, mqtt_client):
        """Test that response message sets the event for matching setting."""
        # Set up pending wait
        mqtt_client._response_event = threading.Event()
        mqtt_client._pending_setting = "ACCharge"

        # Simulate message callback
        msg = MagicMock()
        msg.topic = "dongle-XX:XX:XX:XX:XX:XX/response"
        msg.payload = json.dumps({
            "success": True,
            "setting": "ACCharge",
            "value": "1"
        }).encode()

        mqtt_client._on_message(None, None, msg)

        assert mqtt_client._response_event.is_set()
        assert mqtt_client._last_response["success"] is True
        assert mqtt_client._last_response["setting"] == "ACCharge"

    def test_response_message_ignores_wrong_setting(self, mqtt_client):
        """Test that response for wrong setting doesn't set event."""
        mqtt_client._response_event = threading.Event()
        mqtt_client._pending_setting = "ACCharge"

        msg = MagicMock()
        msg.topic = "dongle-XX:XX:XX:XX:XX:XX/response"
        msg.payload = json.dumps({
            "success": True,
            "setting": "ACChgMode",  # Different setting
            "value": "4"
        }).encode()

        mqtt_client._on_message(None, None, msg)

        assert not mqtt_client._response_event.is_set()  # Should NOT be set

    def test_inputbank1_updates_soc(self, mqtt_client):
        """Test that inputbank1 message updates current SOC."""
        msg = MagicMock()
        msg.topic = "EG4/SERIAL123/inputbank1"
        msg.payload = json.dumps({
            "Serialnumber": "SERIAL123",
            "payload": {
                "SOC": 75,
                "Pcharge": 3500,
                "Pdischarge": 0
            }
        }).encode()

        mqtt_client._on_message(None, None, msg)

        assert mqtt_client.current_soc == 75
        assert mqtt_client.battery_power == 3500
