"""MQTT client for communicating with the solar dongle.

MQTT Protocol:
- Publish ONE setting at a time to /update
- Wait for /response confirmation (up to 15 seconds)
- Only then publish next setting
- Dongle queue limit: 10 settings max
- See ai/about/MQTT_PROTOCOL.md for full protocol documentation
"""

import json
import logging
import time
import threading
from typing import Optional, Callable, Dict, Any, List, Tuple
import paho.mqtt.client as mqtt
from ..models import MQTTConfig

logger = logging.getLogger(__name__)

# Protocol constants
RESPONSE_TIMEOUT_SECONDS = 15.0  # Wait up to 15s for each setting response
PUBLISH_DELAY_FALLBACK = 0.5    # Fallback delay if response wait fails


class MQTTClient:
    """Manages MQTT connection and communication with the solar dongle.

    Implements proper publish/response protocol:
    1. Publish one setting to /update
    2. Wait for /response confirmation
    3. Only then publish next setting

    This prevents queue overflow and ensures reliable delivery.
    """

    def __init__(self, config: MQTTConfig):
        """Initialize MQTT client with configuration."""
        self.config = config
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.current_soc: Optional[int] = None
        self.battery_power: Optional[float] = None
        self.soc_callback: Optional[Callable[[int], None]] = None

        # Response wait mechanism
        self._response_event: Optional[threading.Event] = None
        self._last_response: Optional[Dict[str, Any]] = None
        self._pending_setting: Optional[str] = None  # Track which setting we're waiting for

    def connect(self, timeout: int = 10, retries: int = 3) -> bool:
        """
        Connect to MQTT broker with retry logic.

        Args:
            timeout: Maximum seconds to wait for connection
            retries: Number of connection attempts

        Returns:
            True if connected successfully, False otherwise
        """
        for attempt in range(1, retries + 1):
            try:
                self.client = mqtt.Client(client_id="solar-charging-backend")
                self.client.username_pw_set(self.config.username, self.config.password)

                # Set up callbacks
                self.client.on_connect = self._on_connect
                self.client.on_disconnect = self._on_disconnect
                self.client.on_message = self._on_message

                logger.info(f"Connecting to MQTT broker at {self.config.host}:{self.config.port} (attempt {attempt}/{retries})")
                self.client.connect(self.config.host, self.config.port, keepalive=60)

                # Start network loop in background
                self.client.loop_start()

                # Wait for connection with timeout
                start_time = time.time()
                while not self.connected and (time.time() - start_time) < timeout:
                    time.sleep(0.1)

                if self.connected:
                    logger.info("✅ MQTT connection established successfully")
                    return True
                else:
                    logger.warning(f"❌ MQTT connection timeout on attempt {attempt}/{retries}")
                    if self.client:
                        self.client.loop_stop()

            except Exception as e:
                logger.error(f"❌ Failed to connect to MQTT on attempt {attempt}/{retries}: {e}")
                if self.client:
                    try:
                        self.client.loop_stop()
                    except:
                        pass

            # Wait before retry (except on last attempt)
            if attempt < retries:
                retry_delay = min(attempt * 2, 10)  # Exponential backoff, max 10s
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)

        logger.error(f"❌ Failed to connect to MQTT after {retries} attempts")
        return False

    def disconnect(self):
        """Disconnect from MQTT broker."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logger.info("Disconnected from MQTT broker")

    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to MQTT broker."""
        if rc == 0:
            self.connected = True
            logger.info("Connected to MQTT broker successfully")

            # Subscribe to inputbank1 for power data
            inputbank_topic = f"{self.config.dongle_prefix}/inputbank1"
            client.subscribe(inputbank_topic)
            logger.info(f"Subscribed to {inputbank_topic}")

            # Subscribe to response topic for command confirmations
            response_topic = f"{self.config.dongle_prefix}/response"
            client.subscribe(response_topic)
            logger.info(f"Subscribed to {response_topic}")
        else:
            logger.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Callback when disconnected from MQTT broker."""
        self.connected = False
        if rc != 0:
            logger.warning(f"Unexpected disconnect from MQTT (code {rc}), will auto-reconnect")
        else:
            logger.info("Disconnected from MQTT broker")

    def _on_message(self, client, userdata, msg):
        """Callback when message received from MQTT."""
        topic = msg.topic

        try:
            # Parse inputbank1 for SOC and power data
            if topic.endswith("/inputbank1"):
                payload = json.loads(msg.payload.decode())

                if "Serialnumber" in payload and "payload" in payload:
                    data = payload["payload"]

                    # Extract SOC
                    if "SOC" in data:
                        self.current_soc = int(data["SOC"])
                        logger.debug(f"SOC updated: {self.current_soc}%")

                        # Trigger callback if registered
                        if self.soc_callback:
                            self.soc_callback(self.current_soc)

                    # Extract battery power (Pcharge or Pdischarge)
                    pcharge = data.get("Pcharge", 0)
                    pdischarge = data.get("Pdischarge", 0)

                    if pdischarge > 0:
                        self.battery_power = -pdischarge  # Negative = discharging
                    elif pcharge > 0:
                        self.battery_power = pcharge  # Positive = charging
                    else:
                        self.battery_power = 0

                    logger.debug(f"Battery power: {self.battery_power}W")

            # Parse response topic for command confirmations
            elif topic.endswith("/response"):
                payload = json.loads(msg.payload.decode())
                logger.info(f"Command response: {payload}")

                # Store response and signal waiting thread
                self._last_response = payload
                if self._response_event and self._pending_setting:
                    # Check if this response matches what we're waiting for
                    response_setting = payload.get("setting")
                    if response_setting == self._pending_setting:
                        logger.debug(f"✅ Response received for {self._pending_setting}")
                        self._response_event.set()
                    else:
                        logger.debug(f"Response for {response_setting}, but waiting for {self._pending_setting}")

        except Exception as e:
            logger.error(f"Error parsing MQTT message from {topic}: {e}")

    def _publish_and_wait(self, key: str, value: str, timeout: float = RESPONSE_TIMEOUT_SECONDS) -> bool:
        """
        Publish a single setting and wait for response confirmation.

        This is the core method implementing the MQTT protocol:
        1. Publish setting to /update
        2. Wait for /response with matching setting name
        3. Return True if confirmed, False on timeout or error

        Args:
            key: Setting name (e.g., "ACCharge", "ACChgMode")
            value: Setting value as string
            timeout: Max seconds to wait for response (default 15s)

        Returns:
            True if setting was confirmed, False otherwise
        """
        if not self.connected:
            logger.error("Cannot publish - not connected to MQTT")
            return False

        topic = f"{self.config.dongle_prefix}/update"
        payload = {
            "setting": key,
            "value": value,
            "from": "SolarBackend"
        }

        # Set up response wait
        self._response_event = threading.Event()
        self._pending_setting = key
        self._last_response = None

        try:
            result = self.client.publish(topic, json.dumps(payload))
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(f"Failed to publish {key}: {result.rc}")
                return False

            logger.debug(f"Published {key}={value}, waiting for response...")

            # Wait for response with timeout
            response_received = self._response_event.wait(timeout=timeout)

            if response_received:
                # Check response indicates success
                if self._last_response and self._last_response.get("success") == True:
                    logger.info(f"✅ {key}={value} confirmed by dongle")
                    return True
                else:
                    logger.warning(f"⚠️ {key} response received but not success: {self._last_response}")
                    return False
            else:
                logger.warning(f"⚠️ Timeout waiting for {key} response after {timeout}s")
                return False

        except Exception as e:
            logger.error(f"Error publishing {key}: {e}")
            return False
        finally:
            # Clean up
            self._response_event = None
            self._pending_setting = None

    def publish_settings_sequentially(
        self,
        settings: List[Tuple[str, str]],
        timeout_per_setting: float = RESPONSE_TIMEOUT_SECONDS
    ) -> bool:
        """
        Publish multiple settings sequentially, waiting for each response.

        Args:
            settings: List of (key, value) tuples to publish
            timeout_per_setting: Timeout for each individual setting

        Returns:
            True if all settings were confirmed, False if any failed
        """
        if not settings:
            return True

        all_success = True
        for key, value in settings:
            success = self._publish_and_wait(key, value, timeout_per_setting)
            if not success:
                logger.warning(f"Failed to confirm {key}={value}, continuing with remaining settings")
                all_success = False
                # Add fallback delay before next setting if response wait failed
                time.sleep(PUBLISH_DELAY_FALLBACK)

        return all_success

    def publish_ac_charge_enable(self) -> bool:
        """Publish ACCharge=1 to enable charging with response confirmation."""
        logger.info("Publishing ACCharge=1 (enable charging)")
        return self._publish_and_wait("ACCharge", "1")

    def publish_ac_charge_disable(self) -> bool:
        """Publish ACCharge=0 to disable charging with response confirmation."""
        logger.info("Publishing ACCharge=0 (disable charging)")
        return self._publish_and_wait("ACCharge", "0")

    def publish_time_settings(self, start_time: str, end_time: str) -> bool:
        """
        Publish charging time window settings.

        Uses proper MQTT protocol: publishes each setting sequentially,
        waiting for /response confirmation before sending the next.

        Args:
            start_time: Start time in HH:MM format
            end_time: End time in HH:MM format

        Returns:
            True if all settings confirmed, False if any failed
        """
        settings = [
            ("ACChgStart", start_time),
            ("ACChgEnd", end_time),
            ("ACChgStart1", "00:00"),  # Disable other periods
            ("ACChgEnd1", "00:00"),
            ("ACChgStart2", "00:00"),
            ("ACChgEnd2", "00:00"),
        ]

        logger.info(f"Publishing time settings: {start_time} - {end_time}")
        return self.publish_settings_sequentially(settings)

    def publish_soc_limit(self, target_soc: int) -> bool:
        """Publish ACChgSOCLimit setting with response confirmation."""
        logger.info(f"Publishing ACChgSOCLimit={target_soc}%")
        return self._publish_and_wait("ACChgSOCLimit", str(target_soc))

    def set_soc_callback(self, callback: Callable[[int], None]):
        """Set callback function to be called when SOC is updated."""
        self.soc_callback = callback

    def publish_ac_charge_mode(self, mode: int = 4) -> bool:
        """Publish ACChgMode setting with response confirmation.

        Mode values:
        - 0: Time only (honors time window only)
        - 1: VOLT (voltage-based only)
        - 2: SOC (SOC-based only)
        - 3: Time + VOLT (time window AND voltage limit)
        - 4: Time + SOC (time window AND SOC limit) - DEFAULT

        Without ACChgMode=4, the inverter ignores ACChgSOCLimit entirely.
        """
        mode_names = {
            0: "Time only",
            1: "VOLT only",
            2: "SOC only",
            3: "Time + VOLT",
            4: "Time + SOC"
        }
        mode_name = mode_names.get(mode, "Unknown")
        logger.info(f"Publishing ACChgMode={mode} ({mode_name})")
        return self._publish_and_wait("ACChgMode", str(mode))
