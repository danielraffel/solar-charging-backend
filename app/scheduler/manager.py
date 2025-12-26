"""Charging schedule manager with APScheduler."""

import logging
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import asyncio

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

        # Register SOC callback with MQTT client
        self.mqtt.set_soc_callback(self._on_soc_update)

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
        """Calculate the next occurrence of start_time."""
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
        """Start charging by publishing to MQTT."""
        try:
            # Calculate end time (start + safety cutoff hours)
            hour, minute = map(int, schedule.start_time.split(":"))
            end_hour = (hour + self.config.safety_cutoff_hours) % 24
            end_time = f"{end_hour:02d}:{minute:02d}"

            # Publish settings to dongle
            logger.info("Publishing charging settings to dongle...")

            # 1. Enable AC charging
            if not self.mqtt.publish_ac_charge_enable():
                return False

            await asyncio.sleep(0.5)  # Brief delay between publishes

            # 2. Set time window
            if not self.mqtt.publish_time_settings(schedule.start_time, end_time):
                return False

            await asyncio.sleep(0.5)

            # 3. Set SOC limit
            if not self.mqtt.publish_soc_limit(schedule.target_soc):
                return False

            self.is_charging = True
            self.charge_started_at = datetime.now()
            logger.info("Charging started successfully")
            return True

        except Exception as e:
            logger.error(f"Error starting charge: {e}")
            return False

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

                    # Reschedule if recurring
                    if schedule.mode == "recurring" and self.current_schedule:
                        self._reschedule_recurring(self.current_schedule)

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

        # Reschedule if recurring
        if self.current_schedule and self.current_schedule.mode == "recurring":
            self._reschedule_recurring(self.current_schedule)

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
