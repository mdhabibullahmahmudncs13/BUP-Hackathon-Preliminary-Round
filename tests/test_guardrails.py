"""Guardrail tests: malformed LLM output must be rejected, never coerced."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.models import Directive, InterpretationRequest, ScheduleRequest

adapter = TypeAdapter(Directive)


def valid_directive() -> dict:
    return {"directive_type": "fix_charge", "hours": [1, 2, 3], "factor": 0.5}


def test_valid_directive_parses():
    d = adapter.validate_python(valid_directive())
    assert d.hours == [1, 2, 3]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(hours=[]),                # empty
        lambda d: d.update(hours=[3, 1, 2]),         # not ascending
        lambda d: d.update(hours=[1, 1, 2]),         # duplicate hours
        lambda d: d.update(hours=[-1, 2]),           # out of range (low)
        lambda d: d.update(hours=[22, 23, 24]),      # out of range (high)
        lambda d: d.update(factor=1.5),              # factor > 1
        lambda d: d.update(factor=-0.1),             # factor < 0
        lambda d: d.update(directive_type="unknown"),  # not one of the six
    ],
)
def test_malformed_directives_rejected(mutate):
    raw = valid_directive()
    mutate(raw)
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)


def test_unknown_type_not_coerced_to_noop():
    raw = {"directive_type": "shut_down_everything", "hours": [1]}
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)


def test_fix_price_requires_price():
    raw = {"directive_type": "fix_price", "hours": [1]}
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)


def test_interpretation_request_bounds():
    with pytest.raises(ValidationError):
        InterpretationRequest(notes=[], prices=[1.0] * 24)  # too few notes
    with pytest.raises(ValidationError):
        InterpretationRequest(notes=["a", "b", "c", "d"], prices=[1.0] * 24)  # too many
    with pytest.raises(ValidationError):
        InterpretationRequest(notes=["a"], prices=[1.0] * 23)  # wrong price count
    ok = InterpretationRequest(notes=["a"], prices=[1.0] * 24)
    assert len(ok.prices) == 24


def test_schedule_request_defaults():
    req = ScheduleRequest(directives=[], prices=[10.0] * 24)
    assert req.initial_soc == 0.5
    assert req.capacity_kwh == 10.0
    assert req.power_kw == 5.0
