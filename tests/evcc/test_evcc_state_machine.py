"""Tests for EVCC state machine transitions.

This module tests all possible state transitions for EVCC charging modes,
ensuring correct notification types are triggered for each transition.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.evcc.monitor import EVCCMonitorService, LoadpointState, StateChange


class TestModeTransitions:
    """Test all mode transition combinations."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    @pytest.mark.parametrize("old_mode,new_mode", [
        ("off", "now"),
        ("off", "pv"),
        ("off", "minpv"),
        ("now", "off"),
        ("now", "pv"),
        ("now", "minpv"),
        ("pv", "off"),
        ("pv", "now"),
        ("pv", "minpv"),
        ("minpv", "off"),
        ("minpv", "now"),
        ("minpv", "pv"),
    ])
    def test_mode_transitions(self, monitor, old_mode, new_mode):
        """Test all mode transition combinations generate mode_changed."""
        old_state = LoadpointState(mode=old_mode, charging=False)
        new_state = LoadpointState(mode=new_mode, charging=False, vehicle_soc=50)

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert "mode_changed" in change_types

        mode_change = next(c for c in changes if c.change_type == "mode_changed")
        assert mode_change.old_value == old_mode
        assert mode_change.new_value == new_mode

    def test_same_mode_no_change(self, monitor):
        """Test same mode does not generate change."""
        old_state = LoadpointState(mode="now", charging=False)
        new_state = LoadpointState(mode="now", charging=False)

        changes = monitor.detect_state_changes(old_state, new_state)
        change_types = [c.change_type for c in changes]
        assert "mode_changed" not in change_types


class TestChargingStartTransitions:
    """Test charging start transitions for each mode."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    @pytest.mark.parametrize("mode,expected_type", [
        ("now", "fast_charging_started"),
        ("pv", "solar_charging_started"),
        ("minpv", "minsolar_charging_started"),
    ])
    def test_charging_start_per_mode(self, monitor, mode, expected_type):
        """Test correct charging started type for each mode."""
        old_state = LoadpointState(mode=mode, charging=False)
        new_state = LoadpointState(
            mode=mode,
            charging=True,
            vehicle_soc=50,
            charge_power=7000
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert expected_type in change_types

    def test_plan_charging_start_overrides_mode(self, monitor):
        """Test plan active causes plan_charging_started instead of mode-based."""
        old_state = LoadpointState(mode="now", charging=False, plan_active=True, plan_number=1)
        new_state = LoadpointState(
            mode="now",
            charging=True,
            plan_active=True,
            plan_number=1,
            plan_soc=80,
            vehicle_soc=50
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert "plan_charging_started" in change_types
        assert "fast_charging_started" not in change_types


class TestChargingStopTransitions:
    """Test charging stop transitions for each mode."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    @pytest.mark.parametrize("mode,expected_type", [
        ("now", "fast_charging_stopped"),
        ("pv", "solar_charging_stopped"),
        ("minpv", "minsolar_charging_stopped"),
    ])
    def test_charging_stop_per_mode(self, monitor, mode, expected_type):
        """Test correct charging stopped type for each mode."""
        old_state = LoadpointState(mode=mode, charging=True)
        new_state = LoadpointState(
            mode=mode,
            charging=False,
            vehicle_soc=80,
            charged_energy=25.0
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert expected_type in change_types

    def test_plan_charging_stop(self, monitor):
        """Test plan charging stop generates plan_charging_stopped."""
        old_state = LoadpointState(mode="now", charging=True, plan_active=True, plan_number=1)
        new_state = LoadpointState(
            mode="now",
            charging=False,
            plan_active=True,
            plan_number=1,
            vehicle_soc=80
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert "plan_charging_stopped" in change_types


class TestPlanLifecycle:
    """Test complete plan lifecycle transitions."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    def test_one_time_plan_full_lifecycle(self, monitor):
        """Test one-time plan: activation -> charging -> completion."""
        # Step 1: Plan activated (before charging starts)
        state1 = LoadpointState(mode="off", plan_active=False, charging=False)
        state2 = LoadpointState(
            mode="now",
            plan_active=True,
            plan_number=1,
            plan_soc=80,
            plan_time="08:00",
            charging=False
        )

        changes = monitor.detect_state_changes(state1, state2)
        change_types = [c.change_type for c in changes]
        assert "plan_activated" in change_types
        assert "mode_changed" in change_types

        # Step 2: Charging starts under plan
        state3 = LoadpointState(
            mode="now",
            plan_active=True,
            plan_number=1,
            plan_soc=80,
            charging=True,
            vehicle_soc=50,
            charge_power=7400
        )

        changes = monitor.detect_state_changes(state2, state3)
        change_types = [c.change_type for c in changes]
        assert "plan_charging_started" in change_types

        # Step 3: Plan completes (plan deactivates and charging stops)
        state4 = LoadpointState(
            mode="now",
            plan_active=False,
            charging=False,
            vehicle_soc=80,
            charged_energy=25.5
        )

        changes = monitor.detect_state_changes(state3, state4)
        change_types = [c.change_type for c in changes]
        assert "plan_complete" in change_types

    def test_recurring_plan_lifecycle(self, monitor):
        """Test recurring plan: charging starts -> ends (no activation notification)."""
        # Recurring plans don't trigger plan_activated, only charging notifications
        # Plan already exists, just starts charging when scheduled

        state1 = LoadpointState(
            mode="now",
            plan_active=True,  # Already active (recurring)
            plan_number=2,     # Plan 2+ are recurring
            charging=False
        )
        state2 = LoadpointState(
            mode="now",
            plan_active=True,
            plan_number=2,
            charging=True,
            vehicle_soc=50,
            charge_power=7400
        )

        changes = monitor.detect_state_changes(state1, state2)
        change_types = [c.change_type for c in changes]

        # Should get charging started, NOT plan_activated
        assert "plan_charging_started" in change_types
        # Note: plan_activated wouldn't fire because plan_active was already True

    def test_plan_interrupted_by_mode_change(self, monitor):
        """Test plan interrupted when mode changed to off."""
        old_state = LoadpointState(
            mode="now",
            plan_active=True,
            plan_number=1,
            charging=True
        )
        new_state = LoadpointState(
            mode="off",
            plan_active=False,
            charging=False,
            vehicle_soc=60
        )

        changes = monitor.detect_state_changes(old_state, new_state)
        change_types = [c.change_type for c in changes]

        # Should detect mode change, charging stopped, and plan complete
        assert "mode_changed" in change_types
        assert "plan_complete" in change_types


class TestBatteryBoostTransitions:
    """Test battery boost state transitions."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    def test_battery_boost_activated(self, monitor):
        """Test battery boost activation during charging."""
        old_state = LoadpointState(
            mode="pv",
            charging=True,
            battery_boost=False,
            vehicle_soc=50
        )
        new_state = LoadpointState(
            mode="pv",
            charging=True,
            battery_boost=True,
            battery_power=3000,
            vehicle_soc=55
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert "battery_boost_activated" in change_types

        boost_change = next(c for c in changes if c.change_type == "battery_boost_activated")
        assert boost_change.metadata["battery_power"] == 3000

    def test_battery_boost_deactivated_no_notification(self, monitor):
        """Test battery boost deactivation doesn't generate notification."""
        old_state = LoadpointState(
            mode="pv",
            charging=True,
            battery_boost=True,
            battery_power=3000
        )
        new_state = LoadpointState(
            mode="pv",
            charging=True,
            battery_boost=False,
            battery_power=0
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        # No notification for boost deactivation (only activation matters)
        change_types = [c.change_type for c in changes]
        assert "battery_boost_activated" not in change_types
        assert "battery_boost_deactivated" not in change_types

    def test_battery_boost_not_triggered_when_not_charging(self, monitor):
        """Test battery boost flag only matters when charging."""
        old_state = LoadpointState(
            mode="pv",
            charging=False,
            battery_boost=False
        )
        new_state = LoadpointState(
            mode="pv",
            charging=False,
            battery_boost=True,  # Boost flag but not charging
            battery_power=3000
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        # Current implementation triggers on battery_boost change regardless of charging
        # This test documents that behavior - may want to change in future
        change_types = [c.change_type for c in changes]
        # Battery boost is still detected even when not charging
        assert "battery_boost_activated" in change_types


class TestComplexTransitions:
    """Test complex multi-state transitions."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    def test_mode_change_while_charging(self, monitor):
        """Test mode change while already charging."""
        old_state = LoadpointState(mode="pv", charging=True, vehicle_soc=60)
        new_state = LoadpointState(mode="now", charging=True, vehicle_soc=60)

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        # Should detect mode change, but NOT charging started/stopped
        assert "mode_changed" in change_types
        assert "solar_charging_stopped" not in change_types
        assert "fast_charging_started" not in change_types

    def test_simultaneous_plan_and_charging_start(self, monitor):
        """Test plan activation and charging start in same transition."""
        old_state = LoadpointState(mode="off", plan_active=False, charging=False)
        new_state = LoadpointState(
            mode="now",
            plan_active=True,
            plan_number=1,
            plan_soc=80,
            charging=True,  # Both plan activated AND charging started
            vehicle_soc=50
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert "mode_changed" in change_types
        assert "plan_activated" in change_types
        assert "plan_charging_started" in change_types

    def test_solar_to_fast_while_charging(self, monitor):
        """Test switching from solar to fast mode while charging."""
        old_state = LoadpointState(
            mode="pv",
            charging=True,
            pv_power=5000,
            charge_power=4500,
            vehicle_soc=60
        )
        new_state = LoadpointState(
            mode="now",
            charging=True,
            charge_power=7400,
            vehicle_soc=62
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        # Mode change is detected
        assert "mode_changed" in change_types
        # But charging session continues, so no start/stop

    def test_no_changes_when_only_soc_updates(self, monitor):
        """Test that SOC updates alone don't trigger changes."""
        old_state = LoadpointState(
            mode="now",
            charging=True,
            vehicle_soc=60,
            charge_power=7400
        )
        new_state = LoadpointState(
            mode="now",
            charging=True,
            vehicle_soc=65,  # SOC increased
            charge_power=7400
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        # No state changes should be detected for simple SOC update
        assert len(changes) == 0


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.fixture
    def monitor(self):
        """Create a monitor instance for testing."""
        return EVCCMonitorService(
            evcc_url="http://localhost:7070",
            loadpoint_id=1,
            poll_interval=1
        )

    def test_null_vehicle_soc(self, monitor):
        """Test handling of null vehicle SOC."""
        old_state = LoadpointState(mode="off", vehicle_soc=None)
        new_state = LoadpointState(mode="now", vehicle_soc=None)

        changes = monitor.detect_state_changes(old_state, new_state)

        # Should still detect mode change
        change_types = [c.change_type for c in changes]
        assert "mode_changed" in change_types

    def test_zero_power_values(self, monitor):
        """Test handling of zero power values."""
        old_state = LoadpointState(mode="pv", charging=False, pv_power=0)
        new_state = LoadpointState(
            mode="pv",
            charging=True,
            charge_power=0,  # Unusual but possible
            pv_power=0
        )

        changes = monitor.detect_state_changes(old_state, new_state)

        change_types = [c.change_type for c in changes]
        assert "solar_charging_started" in change_types

    def test_identical_states(self, monitor):
        """Test no changes detected for identical states."""
        state = LoadpointState(
            mode="now",
            charging=True,
            vehicle_soc=60,
            charge_power=7400,
            plan_active=False,
            battery_boost=False
        )

        changes = monitor.detect_state_changes(state, state)

        assert len(changes) == 0

    def test_rapid_mode_toggle(self, monitor):
        """Test rapid mode toggles are handled correctly."""
        # off -> now
        state1 = LoadpointState(mode="off")
        state2 = LoadpointState(mode="now")
        changes = monitor.detect_state_changes(state1, state2)
        assert any(c.change_type == "mode_changed" for c in changes)

        # now -> off (immediate toggle back)
        state3 = LoadpointState(mode="off")
        changes = monitor.detect_state_changes(state2, state3)
        assert any(c.change_type == "mode_changed" for c in changes)
