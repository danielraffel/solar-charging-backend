"""MQTT client for communicating with the solar dongle."""

import json
import logging
import time
from typing import Optional, Callable
import paho.mqtt.client as mqtt
from ..models import MQTTConfig

logger = logging.getLogger(__name__)


class MQTTClient:
    """Manages MQTT connection and communication with the solar dongle."""

    def __init__(self, config: MQTTConfig):
        """Initialize MQTT client with configuration."""
        self.config = config
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.current_soc: Optional[int] = None
        self.battery_power: Optional[float] = None
        self.soc_callback: Optional[Callable[[int], None]] = None

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

        except Exception as e:
            logger.error(f"Error parsing MQTT message from {topic}: {e}")

    def publish_ac_charge_enable(self) -> bool:
        """Publish ACCharge=1 to enable charging."""
        if not self.connected:
            logger.error("Cannot publish - not connected to MQTT")
            return False

        topic = f"{self.config.dongle_prefix}/update"
        payload = {
            "setting": "ACCharge",
            "value": "1",
            "from": "SolarBackend"
        }

        try:
            result = self.client.publish(topic, json.dumps(payload))
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info("Published ACCharge=1 (enable charging)")
                return True
            else:
                logger.error(f"Failed to publish ACCharge: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"Error publishing ACCharge: {e}")
            return False

    def publish_ac_charge_disable(self) -> bool:
        """Publish ACCharge=0 to disable charging."""
        if not self.connected:
            logger.error("Cannot publish - not connected to MQTT")
            return False

        topic = f"{self.config.dongle_prefix}/update"
        payload = {
            "setting": "ACCharge",
            "value": "0",
            "from": "SolarBackend"
        }

        try:
            result = self.client.publish(topic, json.dumps(payload))
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info("Published ACCharge=0 (disable charging)")
                return True
            else:
                logger.error(f"Failed to publish ACCharge: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"Error publishing ACCharge: {e}")
            return False

    def publish_time_settings(self, start_time: str, end_time: str, delay: float = 0.5) -> bool:
        """
        Publish charging time window settings.

        Args:
            start_time: Start time in HH:MM format
            end_time: End time in HH:MM format
            delay: Delay in seconds between each publish (default 0.5s)

        Returns:
            True if all settings published successfully, False otherwise
        """
        if not self.connected:
            logger.error("Cannot publish - not connected to MQTT")
            return False

        topic = f"{self.config.dongle_prefix}/update"
        settings = [
            ("ACChgStart", start_time),
            ("ACChgEnd", end_time),
            ("ACChgStart1", "00:00"),  # Disable other periods
            ("ACChgEnd1", "00:00"),
            ("ACChgStart2", "00:00"),
            ("ACChgEnd2", "00:00"),
        ]

        for i, (key, value) in enumerate(settings):
            payload = {
                "setting": key,
                "value": value,
                "from": "SolarBackend"
            }

            try:
                result = self.client.publish(topic, json.dumps(payload))
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    logger.debug(f"Published {key}={value}")
                else:
                    logger.error(f"Failed to publish {key}: {result.rc}")
                    return False
            except Exception as e:
                logger.error(f"Error publishing {key}: {e}")
                return False

            # Add delay between publishes (except after last one)
            if i < len(settings) - 1:
                time.sleep(delay)

        logger.info(f"Published time settings: {start_time} - {end_time}")
        return True

    def publish_soc_limit(self, target_soc: int) -> bool:
        """Publish ACChgSOCLimit setting."""
        if not self.connected:
            logger.error("Cannot publish - not connected to MQTT")
            return False

        topic = f"{self.config.dongle_prefix}/update"
        payload = {
            "setting": "ACChgSOCLimit",
            "value": str(target_soc),
            "from": "SolarBackend"
        }

        try:
            result = self.client.publish(topic, json.dumps(payload))
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Published ACChgSOCLimit={target_soc}%")
                return True
            else:
                logger.error(f"Failed to publish SOC limit: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"Error publishing SOC limit: {e}")
            return False

    def set_soc_callback(self, callback: Callable[[int], None]):
        """Set callback function to be called when SOC is updated."""
        self.soc_callback = callback
