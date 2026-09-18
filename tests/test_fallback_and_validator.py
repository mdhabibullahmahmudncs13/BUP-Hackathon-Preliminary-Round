"""Tests for the deterministic fallback interpreter and replay validator."""

import pytest

from app.fallback_interpreter import heuristic_interpret
from app.models import DirectiveInterpretation, HourPlan, OptimizeRequest

# ---------------------------------------------------------------------------
# Fallback interpreter
# ---------------------------------------------------------------------------

CASES = [
    # (note, expected_type, expected_hours)
    (
        "Solar output will drop to about 20% from 1 PM to 3 PM.",
        "solar_reduction",
        [13, 14],
    ),
    (
        "Expect an 80% reduction in rooftop solar between 11 AM and 2 PM.",
        "solar_reduction",
        [11, 12, 13],
    ),
    (
        "Do not charge the battery between 2 PM and 4 PM.",
        "no_charge_window",
        [14, 15],
    ),
    (
        "The battery charger will be isolated from 2 AM until 5 AM for maintenance.",
        "no_charge_window",
        [2, 3, 4],
    ),
    (
        "For protection testing, the battery must not discharge from 6 PM until 8 PM.",
        "no_discharge_window",
        [18, 19],
    ),
    (
        "From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour.",
        "max_grid_window",
        [18, 19, 20],
    ),
]


@pytest.mark.parametrize("note,expected_type,expected_hours", CASES)
def test_fallback_parses_directive(note, expected_type, expected_hours):
    results = heuristic_interpret([note], battery_capacity_kwh=200.0)
    assert len(results) == 1
    d = results[0]
    assert d.applies is True
    assert d.directive_type == expected_type
    assert d.structured_adjustment is not None
    assert d.structured_adjustment.hours == expected_hours


def test_fallback_solar_factor_is_remaining_fraction():
    d = heuristic_interpret(["Solar output will drop to about 20% from 1 PM to 3 PM."], 200.0)[0]
    assert d.structured_adjustment.factor == pytest.approx(0.2)


def test_fallback_solar_reduction_wording():
    d = heuristic_interpret(["Expect an 80% reduction in rooftop solar between 11 AM and 2 PM."], 200.0)[0]
    assert d.structured_adjustment.factor == pytest.approx(0.2)


def test_fallback_reserve_kwh():
    d = heuristic_interpret(
        ["Keep at least 90 kWh in the battery from 6 PM until 10 PM for emergencies."], 200.0
    )[0]
    assert d.directive_type == "minimum_battery_reserve"
    assert d.structured_adjustment.minimum_energy_kwh == pytest.approx(90.0)
    assert d.structured_adjustment.hours == [18, 19, 20, 21]  # end-exclusive


def test_fallback_reserve_percent_of_capacity():
    d = heuristic_interpret(
        ["Keep at least 50% of the battery capacity stored from 6 PM until 9 PM."], 200.0
    )[0]
    assert d.directive_type == "minimum_battery_reserve"
    assert d.structured_adjustment.minimum_energy_kwh == pytest.approx(100.0)


def test_fallback_distractor_is_noop():
    d = heuristic_interpret(["The cafeteria menu changes tomorrow."], 200.0)[0]
    assert d.applies is False
    assert d.directive_type == "no_op"
    assert d.structured_adjustment is None


def test_fallback_never_raises_on_garbage():
    results = heuristic_interpret([""], 200.0)
    assert results[0].directive_type == "no_op"


def test_fallback_indices_preserved():
    notes = ["The cafeteria menu changes tomorrow.", "Do not charge between 2 PM and 4 PM."]
    results = heuristic_interpret(notes, 200.0)
    assert [d.note_index for d in results] == [0, 1]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def make_req(**overrides):
    payload = {
        "scenario_id": "V-1",
        "operator_notes": ["n"],
        "hours": [
            {"hour": h, "demand_kwh": 100.0, "solar_kwh": 10.0, "tariff_bdt_per_kwh": 8.0}
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 200.0,
            "initial_energy_kwh": 100.0,
            "minimum_energy_kwh": 40.0,
            "max_charge_kwh_per_hour": 50.0,
            "max_discharge_kwh_per_hour": 50.0,
        },
    }
    payload.update(overrides)
    return OptimizeRequest.model_validate(payload)


def idle_plan(req: OptimizeRequest) -> list[HourPlan]:
    return [
        HourPlan(
            hour=h.hour,
            grid_kwh=h.demand_kwh,
            solar_used_kwh=0.0,
            battery_action="idle",
            battery_kwh=0,
            battery_energy_after_kwh=req.battery.initial_energy_kwh,
        )
        for h in req.hours
    ]


def test_valid_idle_plan_passes():
    req = make_req()
    assert validate_plan(req, [], idle_plan(req)) == []


def test_balance_violation_detected():
    req = make_req()
    plan = idle_plan(req)
    plan[5] = plan[5].model_copy(update={"grid_kwh": plan[5].grid_kwh - 10})
    issues = validate_plan(req, [], plan)
    assert any("balance" in i for i in issues)


def test_solar_overuse_detected():
    req = make_req()
    plan = idle_plan(req)
    plan[5] = plan[5].model_copy(update={"solar_used_kwh": 999.0, "grid_kwh": 90.0})
    issues = validate_plan(req, [], plan)
    assert any("solar" in i for i in issues)


def test_battery_chain_break_detected():
    req = make_req()
    plan = idle_plan(req)
    plan[5] = plan[5].model_copy(
        update={
            "battery_action": "discharge",
            "battery_kwh": 30.0,
            "battery_energy_after_kwh": req.battery.initial_energy_kwh,  # lie
            "grid_kwh": 60.0,
        }
    )
    issues = validate_plan(req, [], plan)
    assert any("chain" in i for i in issues)


def test_neutrality_violation_detected():
    req = make_req()
    plan = idle_plan(req)
    plan[23] = plan[23].model_copy(
        update={
            "battery_action": "discharge",
            "battery_kwh": 10.0,
            "battery_energy_after_kwh": 90.0,
            "grid_kwh": 80.0,
        }
    )
    issues = validate_plan(req, [], plan)
    assert any("end-of-day" in i for i in issues)


def test_reserve_directive_violation_detected():
    from app.models import MinimumBatteryReserveAdjustment

    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="minimum_battery_reserve",
        structured_adjustment=MinimumBatteryReserveAdjustment(hours=[10], minimum_energy_kwh=150.0),
        explanation="",
    )
    req = make_req()
    plan = idle_plan(req)
    plan[10] = plan[10].model_copy(
        update={
            "battery_action": "discharge",
            "battery_kwh": 30.0,
            "battery_energy_after_kwh": 70.0,
            "grid_kwh": 60.0,
        }
    )
    issues = validate_plan(req, [d], plan)
    assert any("below minimum" in i for i in issues)


def test_no_charge_window_violation_detected():
    from app.models import WindowAdjustment

    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_charge_window",
        structured_adjustment=WindowAdjustment(hours=[10]),
        explanation="",
    )
    req = make_req()
    plan = idle_plan(req)
    plan[10] = plan[10].model_copy(
        update={"battery_action": "charge", "battery_kwh": 20.0, "grid_kwh": 110.0}
    )
    issues = validate_plan(req, [d], plan)
    assert any("no_charge_window" in i for i in issues)


from app.validator import validate_plan  # noqa: E402
