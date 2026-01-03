"""Charging schedule manager with APScheduler."""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import asyncio
from dateutil import parser as dateutil_parser

from ..models import ScheduleData, ChargingConfig
from ..mqtt import MQTTClient

logger = logging.getLogger(__name__)


class ChargingScheduleManager:
    """Manages charging schedules and execution logic."""

    def __init__(self, mqtt_client: MQTTClient, charging_config: ChargingConfig):
        """Initialize schedule manager."""
        self.mqtt = mqtt_client
        self.config = charging_config
        self.scheduler = AsyncIOScheduler()
        self.current_schedule: Optional[ScheduleData] = None
        self.is_charging = False
        self.charge_started_at: Optional[datetime] = None
        self.monitoring_task: Optional[asyncio.Task] = None
        self._apns_service = None  # Optional APNs service for push notifications

        # Register SOC callback with MQTT client
        self.mqtt.set_soc_callback(self._on_soc_update)

    def set_apns_service(self, apns_service):
        """Set the APNs service for push notifications.

        Args:
            apns_service: APNsService instance
        """
        self._apns_service = apns_service
        logger.info("APNs service connected to scheduler")

    def start(self):
        """Start the scheduler."""
        self.scheduler.start()
        logger.info("Charging schedule manager started")

    def stop(self):
        """Stop the scheduler and any monitoring tasks."""
        if self.monitoring_task:
            self.monitoring_task.cancel()
        self.scheduler.shutdown()
        logger.info("Charging schedule manager stopped")

    def start_soc_monitoring(self, schedule: ScheduleData):
        """Start SOC monitoring for immediate charging (Phase 2 API).

        Called by /api/charging/enable to monitor SOC and auto-stop when target reached.
        This is the same monitoring used for scheduled charges.
        """
        # Cancel any existing monitoring task
        if self.monitoring_task:
            self.monitoring_task.cancel()
            logger.info("Cancelled previous monitoring task")

        # Start new monitoring task
        self.monitoring_task = asyncio.create_task(self._monitor_charging(schedule))
        logger.info(f"Started SOC monitoring: target={schedule.target_soc}%")

        # Schedule safety cutoff
        cutoff_time = datetime.now() + timedelta(hours=self.config.safety_cutoff_hours)
        self.scheduler.add_job(
            self._safety_cutoff,
            trigger=DateTrigger(run_date=cutoff_time),
            id="safety_cutoff",
            replace_existing=True
        )
        logger.info(f"Safety cutoff scheduled for {cutoff_time}")

    def set_schedule(self, schedule: ScheduleData):
        """Set or update the charging schedule."""
        # Cancel any existing schedule
        self.cancel_schedule()

        self.current_schedule = schedule

        if not schedule.enabled:
            logger.info("Schedule is disabled, not scheduling any jobs")
            return

        # Calculate next run time
        next_run = self._calculate_next_run(schedule.start_time)
        self.current_schedule.next_run = next_run

        # Schedule the job
        self.scheduler.add_job(
            self._execute_charge,
            trigger=DateTrigger(run_date=next_run),
            id="charge_job",
            replace_existing=True,
            args=[schedule]
        )

        logger.info(f"Scheduled charge for {next_run} (mode: {schedule.mode}, target: {schedule.target_soc}%)")

    def cancel_schedule(self):
        """Cancel the current schedule."""
        if self.scheduler.get_job("charge_job"):
            self.scheduler.remove_job("charge_job")
            logger.info("Cancelled scheduled charge")

        # Stop monitoring if active
        if self.monitoring_task:
            self.monitoring_task.cancel()
            self.monitoring_task = None

        # Stop charging if active
        if self.is_charging:
            asyncio.create_task(self._stop_charging())

        self.current_schedule = None

    def _calculate_next_run(self, start_time: str) -> datetime:
        """Calculate the next occurrence of start_time.

        Supports two formats:
        - HH:MM (legacy): Calculates next occurrence in local time
        - ISO 8601 with timezone: Uses provided datetime directly
        """
        # Check if it's ISO 8601 format with timezone
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", start_time):
            # Parse ISO 8601 datetime with timezone
            try:
                dt = dateutil_parser.parse(start_time)
                logger.info(f"Using timezone-aware datetime: {dt} ({dt.tzinfo})")

                # Convert to local time for APScheduler
                local_dt = dt.astimezone()

                # If the time has already passed, return it anyway (for one-time schedules)
                # APScheduler will execute immediately if run_date is in the past
                return local_dt

            except Exception as e:
                logger.error(f"Failed to parse ISO 8601 datetime '{start_time}': {e}")
                # Fall back to HH:MM parsing

        # Legacy HH:MM format - calculate next occurrence
        now = datetime.now()
        hour, minute = map(int, start_time.split(":"))

        # Create datetime for today at start_time
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If time has passed today, schedule for tomorrow
        if next_run <= now:
            next_run += timedelta(days=1)

        return next_run

    async def _execute_charge(self, schedule: ScheduleData):
        """Execute the charging schedule."""
        logger.info(f"Executing charge: target={schedule.target_soc}%, mode={schedule.mode}")

        # Update last run time
        if self.current_schedule:
            self.current_schedule.last_run = datetime.now()

        # Check current SOC
        current_soc = self.mqtt.current_soc
        if current_soc is None:
            logger.warning("Current SOC unknown, starting charge anyway")
        elif current_soc >= schedule.target_soc:
            logger.info(f"SOC ({current_soc}%) already at or above target ({schedule.target_soc}%), skipping charge")

            # Reschedule if recurring
            if schedule.mode == "recurring":
                self._reschedule_recurring(schedule)
            return

        # Start charging
        success = await self._start_charging(schedule)

        if not success:
            logger.error("Failed to start charging")
            return

        # Start monitoring SOC
        self.monitoring_task = asyncio.create_task(self._monitor_charging(schedule))

        # Schedule safety cutoff (stop after X hours regardless of SOC)
        cutoff_time = datetime.now() + timedelta(hours=self.config.safety_cutoff_hours)
        self.scheduler.add_job(
            self._safety_cutoff,
            trigger=DateTrigger(run_date=cutoff_time),
            id="safety_cutoff",
            replace_existing=True
        )

        logger.info(f"Safety cutoff scheduled for {cutoff_time}")

    async def _start_charging(self, schedule: ScheduleData) -> bool:
        """Start charging by publishing to MQTT.

        CRITICAL: Publishes BOTH time window AND SOC limit (hybrid approach).
        Time window is REQUIRED for inverter to start charging (tested and verified).
        - Time window: Tells inverter WHEN to charge (start time → start + 8 hours)
        - SOC limit: Backend monitors and stops when target reached
        """
        try:
            # Publish settings to dongle
            logger.info("Publishing charging settings to dongle...")

            # 1. Enable AC charging (holdbank1)
            if not self.mqtt.publish_ac_charge_enable():
                logger.error("Failed to enable AC charging")
                return False

            await asyncio.sleep(0.5)  # Brief delay between publishes

            # 2. Set time window (start time → start + 8 hours safety cutoff)
            # Extract time from schedule.start_time (supports both HH:MM and ISO 8601)
            start_time_str = self._extract_time_string(schedule.start_time)
            end_time_str = self._calculate_end_time(start_time_str, self.config.safety_cutoff_hours)

            logger.info(f"Setting time window: {start_time_str} → {end_time_str}")
            if not self.mqtt.publish_time_settings(start_time_str, end_time_str):
                logger.error("Failed to publish time window")
                return False

            await asyncio.sleep(0.5)  # Brief delay between publishes

            # 3. Set ACChgMode=4 (Time+SOC mode) so inverter honors SOC limit
            # Without ACChgMode=4, the inverter ignores ACChgSOCLimit entirely
            if not self.mqtt.publish_ac_charge_mode(4):
                logger.error("Failed to set ACChgMode")
                return False

            await asyncio.sleep(0.5)  # Brief delay between publishes

            # 4. Set SOC limit (target percentage)
            if not self.mqtt.publish_soc_limit(schedule.target_soc):
                logger.error("Failed to set SOC limit")
                return False

            self.is_charging = True
            self.charge_started_at = datetime.now()
            logger.info(f"✅ Charging started successfully")
            logger.info(f"   ACChgMode: 4 (Time+SOC)")
            logger.info(f"   Time window: {start_time_str} → {end_time_str} (safety cutoff)")
            logger.info(f"   Target SOC: {schedule.target_soc}%")
            logger.info(f"   Backend will monitor and stop when SOC target reached")
            return True

        except Exception as e:
            logger.error(f"Error starting charge: {e}")
            return False

    def _extract_time_string(self, start_time: str) -> str:
        """Extract HH:MM time string from either HH:MM or ISO 8601 format."""
        # Check if it's ISO 8601 format with timezone
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", start_time):
            # Parse ISO 8601 datetime
            try:
                dt = dateutil_parser.parse(start_time)
                return dt.strftime("%H:%M")
            except Exception as e:
                logger.error(f"Failed to parse datetime '{start_time}': {e}")
                # Fall back to HH:MM parsing or use current time
                return datetime.now().strftime("%H:%M")
        return start_time

    def _calculate_end_time(self, start_time: str, hours_offset: int) -> str:
        """Calculate end time as start time + hours_offset."""
        hour, minute = map(int, start_time.split(":"))
        end_hour = (hour + hours_offset) % 24
        return f"{end_hour:02d}:{minute:02d}"

    async def _stop_charging(self):
        """Stop charging by disabling AC charge."""
        try:
            logger.info("Stopping charging...")
            self.mqtt.publish_ac_charge_disable()
            self.is_charging = False
            self.charge_started_at = None

            # Cancel safety cutoff job if it exists
            if self.scheduler.get_job("safety_cutoff"):
                self.scheduler.remove_job("safety_cutoff")

            logger.info("Charging stopped successfully")

        except Exception as e:
            logger.error(f"Error stopping charge: {e}")

    async def _monitor_charging(self, schedule: ScheduleData):
        """Monitor SOC and stop when target reached."""
        try:
            while self.is_charging:
                await asyncio.sleep(self.config.soc_check_interval)

                current_soc = self.mqtt.current_soc
                if current_soc is None:
                    logger.debug("SOC unknown, continuing to monitor...")
                    continue

                logger.debug(f"Monitoring: current SOC={current_soc}%, target={schedule.target_soc}%")

                if current_soc >= schedule.target_soc:
                    logger.info(f"Target SOC reached ({current_soc}% >= {schedule.target_soc}%)")
                    await self._stop_charging()

                    # Send push notification to iOS app
                    await self._send_charge_complete_notification(
                        target_soc=schedule.target_soc,
                        final_soc=current_soc
                    )

                    # Handle completion based on mode
                    if schedule.mode == "recurring" and self.current_schedule:
                        logger.info("Recurring mode - rescheduling for next day")
                        self._reschedule_recurring(self.current_schedule)
                    elif schedule.mode == "once":
                        logger.info("One-time mode - clearing schedule")
                        self._clear_one_time_schedule()

                    break

        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled")
        except Exception as e:
            logger.error(f"Error in monitoring task: {e}")

    def _on_soc_update(self, soc: int):
        """Callback when SOC is updated via MQTT."""
        logger.debug(f"SOC callback: {soc}%")
        # The main monitoring loop handles SOC checks

    async def _safety_cutoff(self):
        """Safety cutoff - stop charging after maximum duration."""
        logger.warning(f"Safety cutoff triggered after {self.config.safety_cutoff_hours} hours")
        await self._stop_charging()

        # Handle completion based on mode
        if self.current_schedule:
            if self.current_schedule.mode == "recurring":
                logger.info("Recurring mode - rescheduling for next day after safety cutoff")
                self._reschedule_recurring(self.current_schedule)
            elif self.current_schedule.mode == "once":
                logger.info("One-time mode - clearing schedule after safety cutoff")
                self._clear_one_time_schedule()

    def _reschedule_recurring(self, schedule: ScheduleData):
        """Reschedule a recurring charge for the next day."""
        if not schedule.enabled:
            logger.info("Schedule disabled, not rescheduling")
            return

        next_run = self._calculate_next_run(schedule.start_time)
        schedule.next_run = next_run

        self.scheduler.add_job(
            self._execute_charge,
            trigger=DateTrigger(run_date=next_run),
            id="charge_job",
            replace_existing=True,
            args=[schedule]
        )

        logger.info(f"Rescheduled recurring charge for {next_run}")

    def _clear_one_time_schedule(self):
        """Clear one-time schedule after completion."""
        logger.info("Clearing one-time schedule")

        # Cancel any pending scheduled job
        if self.scheduler.get_job("charge_job"):
            self.scheduler.remove_job("charge_job")
            logger.info("Removed scheduled charge job")

        # Clear current schedule
        self.current_schedule = None
        logger.info("One-time schedule cleared - ready for new schedule")

    async def _send_charge_complete_notification(self, target_soc: int, final_soc: int):
        """Send push notification to iOS app that charging is complete.

        Args:
            target_soc: The target SOC that was set
            final_soc: The actual final SOC achieved
        """
        if self._apns_service is None:
            logger.debug("APNs service not configured, skipping notification")
            return

        try:
            sent_count = await self._apns_service.send_charge_complete(target_soc, final_soc)
            if sent_count > 0:
                logger.info(f"Sent charge complete notification to {sent_count} device(s)")
            else:
                logger.debug("No devices registered for push notifications")
        except Exception as e:
            logger.error(f"Failed to send charge complete notification: {e}")
