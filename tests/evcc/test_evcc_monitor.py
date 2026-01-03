"""Tests for EVCC monitor service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.evcc.monitor import EVCCMonitorService, LoadpointState, StateChange


class TestLoadpointState:
    """Tests for LoadpointState dataclass."""

    def test_default_values(self):
        """Test LoadpointState has correct defaults."""
        state = LoadpointState()
        assert state.mode == "off"
        assert state.charging is False
        assert state.vehicle_soc is None
        assert state.charge_power == 0
        assert state.pv_power == 0
        assert state.battery_boost is False
        assert state.plan_active is False

    def test_custom_values(self):
        """Test LoadpointState with custom values."""
        state = LoadpointState(
            mode="now",
            charging=True,
            vehicle_soc=50,
            charge_power=7400,
            pv_power=5000,
        )
        assert state.mode == "now"
        assert state.charging is True
        assert state.vehicle_soc == 50
        assert state.charge_power == 7400
        assert state.pv_power == 5000


class TestStateChange:
    """Tests for StateChange dataclass."""

    def test_state_change_creation(self):
        """Test StateChange creation with metadata."""
        change = StateChange(
            change_type="mode_changed",
            old_value="off",
            new_value="now",
            metadata={"vehicle_soc": 50}
        )
        assert change.change_type == "mode_changed"
        assert change.old_value == "off"
        assert change.new_value == "now"
        assert change.metadata["vehicle_soc"] == 50


class TestEVCCMonitorService:
    """Tests for EVCCMonitorService."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=0,  # 0-indexed
            poll_interval=1
        )

    @pytest.fixture
    def mock_apns(self):
        """Create a mock APNs service."""
        apns = MagicMock()
        apns.send_evcc_mode_changed = AsyncMock(return_value=1)
        apns.send_evcc_plan_activated = AsyncMock(return_value=1)
        apns.send_evcc_plan_charging_started = AsyncMock(return_value=1)
        apns.send_evcc_plan_complete = AsyncMock(return_value=1)
        apns.send_evcc_fast_charging_started = AsyncMock(return_value=1)
        apns.send_evcc_fast_charging_stopped = AsyncMock(return_value=1)
        apns.send_evcc_solar_charging_started = AsyncMock(return_value=1)
        apns.send_evcc_solar_charging_stopped = AsyncMock(return_value=1)
        apns.send_evcc_minsolar_charging_started = AsyncMock(return_value=1)
        apns.send_evcc_minsolar_charging_stopped = AsyncMock(return_value=1)
        apns.send_evcc_battery_boost_activated = AsyncMock(return_value=1)
        return apns

    def test_init(self, monitor):
        """Test monitor initialization."""
        assert monitor.evcc_url == "http://localhost:7070"
        assert monitor.loadpoint_id == 0  # 0-indexed
        assert monitor.poll_interval == 1
        assert monitor._running is False

    # =========================================================================
    # State Change Detection Tests
    # =========================================================================

    def test_detect_no_changes_first_run(self, monitor):
        """Test that first run with None old_state returns no changes."""
        new_state = LoadpointState(mode="off", charging=False)
        changes = monitor.detect_state_changes(None, new_state)
        assert changes == []

    def test_detect_mode_change(self, monitor):
        """Test mode change detection."""
        old_state = LoadpointState(mode="off", charging=False)
        new_state = LoadpointState(mode="now", charging=False, vehicle_soc=50)

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "mode_changed"
        assert changes[0].old_value == "off"
        assert changes[0].new_value == "now"
        assert changes[0].metadata["vehicle_soc"] == 50

    def test_detect_plan_activated(self, monitor):
        """Test one-time plan activation detection."""
        old_state = LoadpointState(plan_active=False)
        new_state = LoadpointState(
            plan_active=True,
            plan_number=1,
            plan_soc=80,
            plan_time="2024-01-15T08:00:00"
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "plan_activated"
        assert changes[0].metadata["plan_number"] == 1
        assert changes[0].metadata["plan_soc"] == 80

    def test_detect_fast_charging_started(self, monitor):
        """Test fast mode charging start detection."""
        old_state = LoadpointState(mode="now", charging=False)
        new_state = LoadpointState(
            mode="now",
            charging=True,
            vehicle_soc=50,
            charge_power=7400
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "fast_charging_started"
        assert changes[0].metadata["charge_power"] == 7400

    def test_detect_fast_charging_stopped(self, monitor):
        """Test fast mode charging stop detection."""
        old_state = LoadpointState(mode="now", charging=True)
        new_state = LoadpointState(
            mode="now",
            charging=False,
            vehicle_soc=80,
            charged_energy=25.5
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "fast_charging_stopped"
        assert changes[0].metadata["final_soc"] == 80

    def test_detect_solar_charging_started(self, monitor):
        """Test solar (PV) mode charging start detection."""
        old_state = LoadpointState(mode="pv", charging=False)
        new_state = LoadpointState(
            mode="pv",
            charging=True,
            vehicle_soc=50,
            charge_power=4500,
            pv_power=5000
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "solar_charging_started"
        assert changes[0].metadata["pv_power"] == 5000

    def test_detect_solar_charging_stopped(self, monitor):
        """Test solar (PV) mode charging stop detection."""
        old_state = LoadpointState(mode="pv", charging=True, pv_power=5000)
        new_state = LoadpointState(
            mode="pv",
            charging=False,
            vehicle_soc=70,
            charged_energy=15.0
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "solar_charging_stopped"
        assert changes[0].metadata["final_soc"] == 70

    def test_detect_minsolar_charging_started(self, monitor):
        """Test min+solar mode charging start detection."""
        old_state = LoadpointState(mode="minpv", charging=False)
        new_state = LoadpointState(
            mode="minpv",
            charging=True,
            vehicle_soc=50,
            charge_power=5000,
            pv_power=3500
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "minsolar_charging_started"

    def test_detect_minsolar_charging_stopped(self, monitor):
        """Test min+solar mode charging stop detection."""
        old_state = LoadpointState(mode="minpv", charging=True)
        new_state = LoadpointState(
            mode="minpv",
            charging=False,
            vehicle_soc=75,
            charged_energy=20.0
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "minsolar_charging_stopped"

    def test_detect_plan_charging_started(self, monitor):
        """Test plan-based charging start detection."""
        old_state = LoadpointState(plan_active=True, plan_number=1, charging=False)
        new_state = LoadpointState(
            plan_active=True,
            plan_number=1,
            plan_soc=80,
            charging=True,
            vehicle_soc=50,
            charge_power=7400
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "plan_charging_started"

    def test_detect_plan_complete(self, monitor):
        """Test plan completion detection."""
        old_state = LoadpointState(plan_active=True, plan_number=1, charging=True)
        new_state = LoadpointState(
            plan_active=False,
            charging=False,
            vehicle_soc=80,
            charged_energy=25.5
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        # Should detect both charging stopped and plan complete
        change_types = [c.change_type for c in changes]
        assert "plan_complete" in change_types

    def test_detect_battery_boost(self, monitor):
        """Test battery boost activation detection."""
        old_state = LoadpointState(battery_boost=False, charging=True)
        new_state = LoadpointState(
            battery_boost=True,
            battery_power=3000,
            vehicle_soc=50,
            charging=True
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        assert len(changes) == 1
        assert changes[0].change_type == "battery_boost_activated"
        assert changes[0].metadata["battery_power"] == 3000

    def test_detect_multiple_changes(self, monitor):
        """Test detection of multiple simultaneous changes."""
        old_state = LoadpointState(mode="off", charging=False, plan_active=False)
        new_state = LoadpointState(
            mode="now",
            charging=True,
            plan_active=True,
            plan_number=1,
            vehicle_soc=50
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        # Should detect mode change, plan activated, and charging started
        change_types = [c.change_type for c in changes]
        assert "mode_changed" in change_types
        assert "plan_activated" in change_types
        assert "plan_charging_started" in change_types

    # =========================================================================
    # Charging Type Helper Tests
    # =========================================================================

    def test_get_charging_started_type_fast(self, monitor):
        """Test correct charging started type for fast mode."""
        state = LoadpointState(mode="now")
        assert monitor._get_charging_started_type(state) == "fast_charging_started"

    def test_get_charging_started_type_solar(self, monitor):
        """Test correct charging started type for solar mode."""
        state = LoadpointState(mode="pv")
        assert monitor._get_charging_started_type(state) == "solar_charging_started"

    def test_get_charging_started_type_minsolar(self, monitor):
        """Test correct charging started type for min+solar mode."""
        state = LoadpointState(mode="minpv")
        assert monitor._get_charging_started_type(state) == "minsolar_charging_started"

    def test_get_charging_started_type_plan(self, monitor):
        """Test correct charging started type for plan mode."""
        state = LoadpointState(mode="now", plan_active=True)
        assert monitor._get_charging_started_type(state) == "plan_charging_started"

    def test_get_charging_stopped_type_fast(self, monitor):
        """Test correct charging stopped type for fast mode."""
        state = LoadpointState(mode="now")
        assert monitor._get_charging_stopped_type(state) == "fast_charging_stopped"

    def test_get_charging_stopped_type_solar(self, monitor):
        """Test correct charging stopped type for solar mode."""
        state = LoadpointState(mode="pv")
        assert monitor._get_charging_stopped_type(state) == "solar_charging_stopped"

    def test_get_charging_stopped_type_minsolar(self, monitor):
        """Test correct charging stopped type for min+solar mode."""
        state = LoadpointState(mode="minpv")
        assert monitor._get_charging_stopped_type(state) == "minsolar_charging_stopped"

    # =========================================================================
    # Properties Tests
    # =========================================================================

    def test_is_running_property(self, monitor):
        """Test is_running property."""
        assert monitor.is_running is False
        monitor._running = True
        assert monitor.is_running is True

    def test_last_state_property(self, monitor):
        """Test last_state property."""
        assert monitor.last_state is None
        test_state = LoadpointState(mode="now")
        monitor._last_state = test_state
        assert monitor.last_state == test_state

    def test_set_apns_service(self, monitor, mock_apns):
        """Test setting APNs service."""
        assert monitor.apns_service is None
        monitor.set_apns_service(mock_apns)
        assert monitor.apns_service == mock_apns


class TestEVCCMonitorFetchState:
    """Tests for fetch_evcc_state method."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=0,  # 0-indexed
            poll_interval=1
        )

    @pytest.mark.asyncio
    async def test_fetch_state_no_client(self, monitor):
        """Test fetch returns None when client not initialized."""
        result = await monitor.fetch_evcc_state()
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_state_success(self, monitor):
        """Test successful state fetch from EVCC API."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "loadpoints": [{
                "mode": "now",
                "charging": True,
                "vehicleSoc": 50,
                "chargePower": 7400,
                "planActive": False,
                "chargedEnergy": 5000,  # Wh
            }],
            "pvPower": 3000,
            "batteryPower": -1000,  # Negative = discharging
            "batterySoc": 80,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        monitor._http_client = mock_client

        state = await monitor.fetch_evcc_state()

        assert state is not None
        assert state.mode == "now"
        assert state.charging is True
        assert state.vehicle_soc == 50
        assert state.charge_power == 7400
        assert state.pv_power == 3000
        assert state.charged_energy == 5.0  # Converted to kWh

    @pytest.mark.asyncio
    async def test_fetch_state_loadpoint_not_found(self, monitor):
        """Test fetch returns None when loadpoint not found."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "loadpoints": [],  # No loadpoints
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        monitor._http_client = mock_client

        state = await monitor.fetch_evcc_state()
        assert state is None

    @pytest.mark.asyncio
    async def test_fetch_state_with_plan(self, monitor):
        """Test fetch with active plan."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "loadpoints": [{
                "mode": "now",
                "charging": True,
                "vehicleSoc": 50,
                "chargePower": 7400,
                "planActive": True,
                "planSoc": 80,
                "planTime": "2024-01-15T08:00:00",
                "plans": [{"soc": 80}],
                "chargedEnergy": 0,
            }],
            "pvPower": 0,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        monitor._http_client = mock_client

        state = await monitor.fetch_evcc_state()

        assert state is not None
        assert state.plan_active is True
        assert state.plan_soc == 80
        assert state.plan_time == "2024-01-15T08:00:00"
        assert state.plan_number == 1

    @pytest.mark.asyncio
    async def test_fetch_state_battery_boost_detected(self, monitor):
        """Test battery boost detection during fetch."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "loadpoints": [{
                "mode": "now",
                "charging": True,
                "vehicleSoc": 50,
                "chargePower": 7400,
                "planActive": False,
                "chargedEnergy": 0,
            }],
            "pvPower": 0,
            "batteryPower": -3000,  # Discharging 3kW to EV
            "batterySoc": 80,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        monitor._http_client = mock_client

        state = await monitor.fetch_evcc_state()

        assert state is not None
        assert state.battery_boost is True
        assert state.battery_power == 3000  # Absolute value
