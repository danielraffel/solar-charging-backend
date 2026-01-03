"""EVCC state monitoring service for EV charging Live Activities."""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LoadpointState:
    """State snapshot for an EVCC loadpoint."""
    mode: str = "off"  # off, now, pv, minpv
    charging: bool = False
    vehicle_soc: Optional[int] = None
    charge_power: int = 0  # Watts
    pv_power: int = 0  # Solar power in Watts
    battery_boost: bool = False
    battery_power: int = 0  # Home battery power
    plan_active: bool = False
    plan_number: Optional[int] = None
    plan_soc: Optional[int] = None
    plan_time: Optional[str] = None
    charged_energy: float = 0.0  # kWh in current session
    session_start: Optional[datetime] = None


@dataclass
class StateChange:
    """Represents a detected state change."""
    change_type: str
    old_value: Any = None
    new_value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class EVCCMonitorService:
    """Service to monitor EVCC state and detect changes for notifications."""

    def __init__(
        self,
        evcc_url: str,
        loadpoint_id: int = 1,
        poll_interval: int = 30,
        apns_service: Optional[Any] = None
    ):
        """Initialize EVCC monitor.

        Args:
            evcc_url: Base URL of EVCC server (e.g., http://192.168.86.68:7070)
            loadpoint_id: Which loadpoint to monitor (default 1)
            poll_interval: Seconds between state polls
            apns_service: Optional APNsService for sending notifications
        """
        self.evcc_url = evcc_url.rstrip('/')
        self.loadpoint_id = loadpoint_id
        self.poll_interval = poll_interval
        self.apns_service = apns_service

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_state: Optional[LoadpointState] = None
        self._http_client: Optional[httpx.AsyncClient] = None

        # Track session metrics
        self._session_start_soc: Optional[int] = None
        self._session_start_time: Optional[datetime] = None

    async def start(self):
        """Start the monitoring background task."""
        if self._running:
            logger.warning("EVCC monitor already running")
            return

        self._running = True
        self._http_client = httpx.AsyncClient(timeout=10.0)
        self._task = asyncio.create_task(self._monitoring_loop())
        logger.info(f"EVCC monitor started: {self.evcc_url} (loadpoint {self.loadpoint_id})")

    async def stop(self):
        """Stop the monitoring background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("EVCC monitor stopped")

    async def _monitoring_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                state = await self.fetch_evcc_state()
                if state:
                    changes = self.detect_state_changes(self._last_state, state)
                    if changes:
                        await self._process_changes(changes, state)
                    self._last_state = state
            except Exception as e:
                logger.error(f"Error in EVCC monitoring loop: {e}")

            await asyncio.sleep(self.poll_interval)

    async def fetch_evcc_state(self) -> Optional[LoadpointState]:
        """Fetch current state from EVCC API.

        Returns:
            LoadpointState if successful, None on error
        """
        if not self._http_client:
            return None

        try:
            # Fetch main state
            response = await self._http_client.get(f"{self.evcc_url}/api/state")
            response.raise_for_status()
            data = response.json()

            # Get loadpoint data (EVCC returns loadpoints at root level)
            loadpoints = data.get("loadpoints", [])
            if self.loadpoint_id >= len(loadpoints):
                logger.error(f"Loadpoint {self.loadpoint_id} not found (only {len(loadpoints)} available)")
                return None

            lp = loadpoints[self.loadpoint_id]  # 0-indexed

            # Get battery data for boost detection (at root level)
            battery_power = data.get("batteryPower", 0)
            home_soc = data.get("batterySoc", 0)

            # Get PV power
            pv_power = data.get("pvPower", 0)

            # Parse plan info
            plan_active = lp.get("planActive", False)
            plan_number = None
            plan_soc = None
            plan_time = None

            if plan_active:
                plan_soc = lp.get("planSoc")
                plan_time = lp.get("planTime")
                # Determine plan number from plan data
                plans = lp.get("plans", [])
                if plans:
                    plan_number = 1  # Default to plan 1

            # Detect battery boost (home battery discharging to EV)
            battery_boost = (
                lp.get("charging", False) and
                battery_power < -500  # Negative = discharging
            )

            state = LoadpointState(
                mode=lp.get("mode", "off"),
                charging=lp.get("charging", False),
                vehicle_soc=lp.get("vehicleSoc"),
                charge_power=int(lp.get("chargePower", 0)),
                pv_power=int(pv_power),
                battery_boost=battery_boost,
                battery_power=int(abs(battery_power)) if battery_boost else 0,
                plan_active=plan_active,
                plan_number=plan_number,
                plan_soc=plan_soc,
                plan_time=plan_time,
                charged_energy=lp.get("chargedEnergy", 0) / 1000,  # Wh to kWh
            )

            return state

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching EVCC state: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing EVCC state: {e}")
            return None

    def detect_state_changes(
        self,
        old_state: Optional[LoadpointState],
        new_state: LoadpointState
    ) -> List[StateChange]:
        """Compare old and new state to detect changes.

        Args:
            old_state: Previous state (None on first run)
            new_state: Current state

        Returns:
            List of detected state changes
        """
        changes: List[StateChange] = []

        if old_state is None:
            # First run - no changes to detect
            return changes

        # Mode change detection
        if old_state.mode != new_state.mode:
            changes.append(StateChange(
                change_type="mode_changed",
                old_value=old_state.mode,
                new_value=new_state.mode,
                metadata={"vehicle_soc": new_state.vehicle_soc}
            ))

        # Plan activation (one-time plans)
        if not old_state.plan_active and new_state.plan_active:
            changes.append(StateChange(
                change_type="plan_activated",
                metadata={
                    "plan_number": new_state.plan_number,
                    "plan_soc": new_state.plan_soc,
                    "plan_time": new_state.plan_time,
                }
            ))

        # Charging state changes
        if not old_state.charging and new_state.charging:
            # Charging started
            change_type = self._get_charging_started_type(new_state)
            changes.append(StateChange(
                change_type=change_type,
                metadata={
                    "mode": new_state.mode,
                    "vehicle_soc": new_state.vehicle_soc,
                    "charge_power": new_state.charge_power,
                    "pv_power": new_state.pv_power,
                    "plan_number": new_state.plan_number,
                    "plan_soc": new_state.plan_soc,
                }
            ))

        elif old_state.charging and not new_state.charging:
            # Charging stopped
            change_type = self._get_charging_stopped_type(old_state)
            duration_minutes = None
            if self._session_start_time:
                duration_minutes = int((datetime.now() - self._session_start_time).total_seconds() / 60)

            changes.append(StateChange(
                change_type=change_type,
                metadata={
                    "mode": old_state.mode,
                    "final_soc": new_state.vehicle_soc,
                    "charged_energy": new_state.charged_energy,
                    "duration_minutes": duration_minutes,
                    "plan_number": old_state.plan_number,
                    "pv_power": old_state.pv_power,
                }
            ))

        # Plan completion
        if old_state.plan_active and not new_state.plan_active and not new_state.charging:
            changes.append(StateChange(
                change_type="plan_complete",
                metadata={
                    "plan_number": old_state.plan_number,
                    "final_soc": new_state.vehicle_soc,
                    "charged_energy": new_state.charged_energy,
                }
            ))

        # Battery boost detection
        if not old_state.battery_boost and new_state.battery_boost:
            changes.append(StateChange(
                change_type="battery_boost_activated",
                metadata={
                    "vehicle_soc": new_state.vehicle_soc,
                    "battery_power": new_state.battery_power,
                }
            ))

        return changes

    def _get_charging_started_type(self, state: LoadpointState) -> str:
        """Determine the appropriate charging started event type."""
        if state.plan_active:
            return "plan_charging_started"
        elif state.mode == "now":
            return "fast_charging_started"
        elif state.mode == "pv":
            return "solar_charging_started"
        elif state.mode == "minpv":
            return "minsolar_charging_started"
        else:
            return "charging_started"

    def _get_charging_stopped_type(self, state: LoadpointState) -> str:
        """Determine the appropriate charging stopped event type."""
        if state.plan_active:
            return "plan_charging_stopped"
        elif state.mode == "now":
            return "fast_charging_stopped"
        elif state.mode == "pv":
            return "solar_charging_stopped"
        elif state.mode == "minpv":
            return "minsolar_charging_stopped"
        else:
            return "charging_stopped"

    async def _process_changes(self, changes: List[StateChange], state: LoadpointState):
        """Process detected changes and send notifications."""
        for change in changes:
            logger.info(f"EVCC state change: {change.change_type} - {change.metadata}")

            # Track session start
            if "charging_started" in change.change_type:
                self._session_start_time = datetime.now()
                self._session_start_soc = state.vehicle_soc

            # Send notifications via APNs
            if self.apns_service:
                await self._send_notification(change, state)

    async def _send_notification(self, change: StateChange, state: LoadpointState):
        """Send appropriate APNs notification for a state change."""
        if not self.apns_service:
            return

        meta = change.metadata

        try:
            if change.change_type == "mode_changed":
                await self.apns_service.send_evcc_mode_changed(
                    previous_mode=change.old_value,
                    new_mode=change.new_value,
                    vehicle_soc=meta.get("vehicle_soc", 0)
                )

            elif change.change_type == "plan_activated":
                await self.apns_service.send_evcc_plan_activated(
                    plan_number=meta.get("plan_number", 1),
                    departure_time=meta.get("plan_time", ""),
                    target_soc=meta.get("plan_soc", 100)
                )

            elif change.change_type == "plan_charging_started":
                await self.apns_service.send_evcc_plan_charging_started(
                    plan_number=meta.get("plan_number", 1),
                    departure_time=meta.get("plan_time", ""),
                    target_soc=meta.get("plan_soc", 100),
                    charging_power=meta.get("charge_power", 0),
                    mode=meta.get("mode", "now")
                )

            elif change.change_type == "plan_complete":
                await self.apns_service.send_evcc_plan_complete(
                    plan_number=meta.get("plan_number", 1),
                    final_soc=meta.get("final_soc", 0),
                    charged_kwh=meta.get("charged_energy", 0)
                )

            elif change.change_type == "fast_charging_started":
                await self.apns_service.send_evcc_fast_charging_started(
                    current_soc=meta.get("vehicle_soc", 0),
                    charging_power=meta.get("charge_power", 0)
                )

            elif change.change_type == "fast_charging_stopped":
                await self.apns_service.send_evcc_fast_charging_stopped(
                    final_soc=meta.get("final_soc", 0),
                    charged_kwh=meta.get("charged_energy", 0),
                    duration_minutes=meta.get("duration_minutes", 0)
                )

            elif change.change_type == "solar_charging_started":
                await self.apns_service.send_evcc_solar_charging_started(
                    current_soc=meta.get("vehicle_soc", 0),
                    solar_power=meta.get("pv_power", 0),
                    charging_power=meta.get("charge_power", 0)
                )

            elif change.change_type == "solar_charging_stopped":
                await self.apns_service.send_evcc_solar_charging_stopped(
                    final_soc=meta.get("final_soc", 0),
                    charged_kwh=meta.get("charged_energy", 0),
                    solar_percentage=0.0  # TODO: Calculate from session
                )

            elif change.change_type == "minsolar_charging_started":
                await self.apns_service.send_evcc_minsolar_charging_started(
                    current_soc=meta.get("vehicle_soc", 0),
                    min_power=1400,  # Typical L1 minimum
                    solar_power=meta.get("pv_power", 0),
                    charging_power=meta.get("charge_power", 0)
                )

            elif change.change_type == "minsolar_charging_stopped":
                await self.apns_service.send_evcc_minsolar_charging_stopped(
                    final_soc=meta.get("final_soc", 0),
                    charged_kwh=meta.get("charged_energy", 0)
                )

            elif change.change_type == "battery_boost_activated":
                # Get home battery SOC from somewhere (may need to pass in)
                await self.apns_service.send_evcc_battery_boost_activated(
                    vehicle_soc=meta.get("vehicle_soc", 0),
                    battery_power=meta.get("battery_power", 0),
                    home_soc=0  # TODO: Get from state
                )

        except Exception as e:
            logger.error(f"Error sending EVCC notification ({change.change_type}): {e}")

    def set_apns_service(self, apns_service):
        """Set the APNs service for notifications."""
        self.apns_service = apns_service

    @property
    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running

    @property
    def last_state(self) -> Optional[LoadpointState]:
        """Get the last known state."""
        return self._last_state
