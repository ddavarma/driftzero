import pytest

from fleet_telemetry.charging import charge_minutes, plan_charging


def test_charge_minutes():
    assert charge_minutes(0.35) == pytest.approx(50.0)
    assert charge_minutes(0.99) == 0.0


def test_plan_charging_emptiest_first_and_shares_docks():
    levels = {"amr-01": 0.82, "amr-04": 0.11, "amr-05": 0.25, "amr-06": 0.20}
    slots = plan_charging(levels, docks=2)
    assert [s.robot_id for s in slots] == ["amr-04", "amr-06", "amr-05"]
    assert (slots[0].dock, slots[1].dock, slots[2].dock) == (0, 1, 1)
    assert slots[2].start_min == pytest.approx(slots[1].minutes)
