"""Optimizer tests: MILP correctness, directive effects, replay integrity."""

import pytest

from app.models import (
    DirectiveInterpretation,
    HourPlan,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    OptimizeRequest,
    SolarReductionAdjustment,
    WindowAdjustment,
)
from app.optimizer import optimize_schedule
from app.validator import validate_plan


def make_request(directives=None, **overrides):
    payload = {
        "scenario_id": "OPT-1",
        "operator_notes": ["n0", "n1", "n2"],
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
    return OptimizeRequest.model_validate(payload), directives or []


def peak_hours(request: OptimizeRequest, count: int = 2) -> list[int]:
    """Indices of the `count` most expensive hours."""
    tariffs = [h.tariff_bdt_per_kwh for h in request.hours]
    return sorted(sorted(range(24), key=lambda h: tariffs[h], reverse=True)[:count])


def test_flat_prices_idle_plan_is_valid():
    req, dirs = make_request()
    plan, relaxed = optimize_schedule(req, dirs)
    assert relaxed == frozenset()
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan]) == []
    # Flat prices -> no arbitrage incentive -> idle battery, solar only.
    assert all(p["battery_action"] == "idle" for p in plan)
    assert all(p["grid_kwh"] == pytest.approx(90.0, abs=0.01) for p in plan)


def test_arbitrage_shifts_energy_to_expensive_hours():
    tariffs = [8.0] * 12 + [30.0] * 12
    req, dirs = make_request(
        overrides_hours=None,
        **{
            "hours": [
                {"hour": h, "demand_kwh": 100.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": t}
                for h, t in enumerate(tariffs)
            ]
        },
    )
    plan, _ = optimize_schedule(req, dirs)
    peak = peak_hours(req, 2)
    assert all(plan[h]["battery_action"] == "discharge" for h in peak)
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan]) == []


def test_solar_reduction_forces_extra_grid_in_window():
    req, _ = make_request()
    base_plan, _ = optimize_schedule(req, [])
    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="solar_reduction",
        structured_adjustment=SolarReductionAdjustment(hours=[10], factor=0.0),
        explanation="",
    )
    req2, dirs = make_request([d])
    reduced_plan, _ = optimize_schedule(req2, dirs)
    base = next(p for p in base_plan if p["hour"] == 10)
    reduced = next(p for p in reduced_plan if p["hour"] == 10)
    assert reduced["grid_kwh"] == pytest.approx(base["grid_kwh"] + 10.0, abs=0.01)


def test_no_charge_window_respected():
    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_charge_window",
        structured_adjustment=WindowAdjustment(hours=[2, 3]),
        explanation="",
    )
    req, dirs = make_request([d])
    plan, _ = optimize_schedule(req, dirs)
    assert all(plan[h]["battery_action"] != "charge" for h in (2, 3))
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan]) == []


def test_no_discharge_window_respected():
    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_discharge_window",
        structured_adjustment=WindowAdjustment(hours=[12, 13]),
        explanation="",
    )
    req, dirs = make_request([d])
    plan, _ = optimize_schedule(req, dirs)
    assert all(plan[h]["battery_action"] != "discharge" for h in (12, 13))
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan]) == []


def test_minimum_reserve_blocks_deep_discharge():
    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="minimum_battery_reserve",
        structured_adjustment=MinimumBatteryReserveAdjustment(hours=[12], minimum_energy_kwh=150.0),
        explanation="",
    )
    req, dirs = make_request([d])
    plan, _ = optimize_schedule(req, dirs)
    assert plan[12]["battery_energy_after_kwh"] >= 150.0 - 0.01
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan]) == []


def test_grid_cap_respected_and_binding():
    tariffs = [8.0] * 23 + [100.0]
    hours = [
        {"hour": h, "demand_kwh": 120.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": t}
        for h, t in enumerate(tariffs)
    ]
    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="max_grid_window",
        structured_adjustment=MaxGridWindowAdjustment(hours=[23], max_grid_kwh=80.0),
        explanation="",
    )
    req, dirs = make_request([d], hours=hours)
    plan, _ = optimize_schedule(req, dirs)
    assert plan[23]["grid_kwh"] <= 80.0 + 0.01
    assert plan[23]["battery_kwh"] >= 39.0  # forced to cover the rest (demand 120 - cap 80)
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan]) == []


def test_end_of_day_neutrality_always():
    tariffs = [8.0] * 12 + [30.0] * 12
    hours = [
        {"hour": h, "demand_kwh": 100.0, "solar_kwh": 20.0, "tariff_bdt_per_kwh": t}
        for h, t in enumerate(tariffs)
    ]
    req, dirs = make_request(hours=hours)
    plan, _ = optimize_schedule(req, dirs)
    assert plan[-1]["battery_energy_after_kwh"] == pytest.approx(100.0, abs=0.01)


def test_relaxation_ladder_kicks_in_on_impossible_directive():
    """A reserve above capacity can't hold; the ladder must still emit a valid plan."""
    d = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="minimum_battery_reserve",
        structured_adjustment=MinimumBatteryReserveAdjustment(hours=[12], minimum_energy_kwh=5000.0),
        explanation="",
    )
    req, dirs = make_request([d])
    plan, relaxed = optimize_schedule(req, dirs)
    assert "reserve" in relaxed or "all" in relaxed
    assert validate_plan(req, dirs, [HourPlan.model_validate(p) for p in plan], relaxed) == []
