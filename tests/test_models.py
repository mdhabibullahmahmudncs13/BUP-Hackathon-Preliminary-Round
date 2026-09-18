"""Guardrail tests for the GridWise directive models (Problem Statement §05/§08)."""

import pytest
from pydantic import ValidationError

from app.models import (
    DirectiveInterpretation,
    OptimizeRequest,
    SolarReductionAdjustment,
    WindowAdjustment,
)


def make_request(**overrides):
    payload = {
        "scenario_id": "T-1",
        "operator_notes": ["note one"],
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


# ---------------------------------------------------------------------------
# hours guardrails
# ---------------------------------------------------------------------------


def test_hours_valid_ascending():
    adj = WindowAdjustment(hours=[2, 3, 4])
    assert adj.hours == [2, 3, 4]


@pytest.mark.parametrize(
    "hours",
    [
        [],  # empty
        [3, 1, 2],  # not ascending
        [1, 1, 2],  # duplicates
        [-1, 2],  # below range
        [22, 23, 24],  # above range
        [1, 2.5],  # non-integer
    ],
)
def test_hours_malformed_rejected(hours):
    with pytest.raises(ValidationError):
        WindowAdjustment(hours=hours)


def test_adjustment_rejects_extra_fields():
    with pytest.raises(ValidationError):
        WindowAdjustment(hours=[1, 2], max_grid_kwh=100.0)


# ---------------------------------------------------------------------------
# numeric guardrails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factor", [0.0, 1.0, 0.25, 0.5])
def test_factor_valid_range(factor):
    assert SolarReductionAdjustment(hours=[12], factor=factor).factor == factor


@pytest.mark.parametrize("factor", [-0.01, 1.01, 1.5])
def test_factor_out_of_range_rejected(factor):
    with pytest.raises(ValidationError):
        SolarReductionAdjustment(hours=[12], factor=factor)


def test_negative_reserve_rejected():
    with pytest.raises(ValidationError):
        from app.models import MinimumBatteryReserveAdjustment

        MinimumBatteryReserveAdjustment(hours=[18], minimum_energy_kwh=-5)


def test_negative_grid_cap_rejected():
    with pytest.raises(ValidationError):
        from app.models import MaxGridWindowAdjustment

        MaxGridWindowAdjustment(hours=[18], max_grid_kwh=-1)


# ---------------------------------------------------------------------------
# DirectiveInterpretation semantics
# ---------------------------------------------------------------------------


def test_noop_requires_applies_false_and_null_adjustment():
    with pytest.raises(ValidationError):
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="no_op", explanation=""
        )
    with pytest.raises(ValidationError):
        DirectiveInterpretation(
            note_index=0,
            applies=False,
            directive_type="no_op",
            structured_adjustment={"hours": [1]},
            explanation="",
        )
    ok = DirectiveInterpretation(
        note_index=0, applies=False, directive_type="no_op", structured_adjustment=None, explanation="x"
    )
    assert ok.applies is False


def test_non_noop_requires_applies_true_and_adjustment():
    with pytest.raises(ValidationError):
        DirectiveInterpretation(
            note_index=0, applies=False, directive_type="solar_reduction", explanation=""
        )
    with pytest.raises(ValidationError):
        DirectiveInterpretation(
            note_index=0, applies=True, directive_type="solar_reduction", explanation=""
        )


def test_adjustment_shape_must_match_directive_type():
    # no_charge_window must not carry a max_grid_kwh payload.
    with pytest.raises(ValidationError):
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="no_charge_window",
            structured_adjustment={"hours": [1], "max_grid_kwh": 100.0},
            explanation="",
        )


def test_unknown_directive_type_rejected():
    with pytest.raises(ValidationError):
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="fix_price",
            structured_adjustment={"hours": [1]},
            explanation="",
        )


# ---------------------------------------------------------------------------
# Request guardrails
# ---------------------------------------------------------------------------


def test_request_rejects_empty_note():
    with pytest.raises(ValidationError):
        make_request(operator_notes=["ok note", "   "])


def test_request_note_count_bounds():
    with pytest.raises(ValidationError):
        make_request(operator_notes=[])
    with pytest.raises(ValidationError):
        make_request(operator_notes=["a", "b", "c", "d"])


def test_request_requires_24_unique_hours():
    bad_hours = [
        {"hour": h, "demand_kwh": 100.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0}
        for h in range(23)
    ]
    with pytest.raises(ValidationError):
        make_request(hours=bad_hours)


def test_request_rejects_initial_energy_over_capacity():
    battery = {
        "capacity_kwh": 100.0,
        "initial_energy_kwh": 150.0,
        "minimum_energy_kwh": 40.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    }
    with pytest.raises(ValidationError):
        make_request(battery=battery)


def test_effective_solar_applies_reduction():
    req = make_request()
    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="solar_reduction",
            structured_adjustment={"hours": [10, 11], "factor": 0.5},
            explanation="",
        )
    ]
    solar = req.effective_solar(directives)
    assert solar[10] == pytest.approx(5.0)
    assert solar[9] == pytest.approx(10.0)
