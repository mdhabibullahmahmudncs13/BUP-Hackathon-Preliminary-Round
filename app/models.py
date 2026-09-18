"""Pydantic v2 guardrail models for LLM output.

The same models serve three purposes:
1. Request/response schema for the FastAPI endpoints.
2. Validation of anything the LLM returns (malformed output -> rejected, never coerced).
3. Documentation via /docs.

Design rule (from the challenge rubric): SAFE FAILURE over silent invention.
Anything that does not parse into one of the six strict models is rejected
with an explicit error, not "fixed" by guessing.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

DirectiveType = Literal[
    "fix_price",  # placeholder names -- align with the actual challenge spec
    "fix_charge",
    "fix_discharge",
    "cap_charge",
    "cap_discharge",
    "no_op",
]

Hour = Annotated[int, Field(ge=0, le=23)]
Factor = Annotated[float, Field(ge=0.0, le=1.0)]


class _HoursDirective(BaseModel):
    """Base for directives that constrain a set of hours."""

    hours: list[Hour]

    @model_validator(mode="after")
    def _check_hours(self) -> "_HoursDirective":
        if not self.hours:
            raise ValueError("hours must be a non-empty list")
        if len(set(self.hours)) != len(self.hours):
            raise ValueError("hours must be unique (no repeated hours)")
        if self.hours != sorted(self.hours):
            raise ValueError("hours must be in ascending order")
        return self


class FixPriceDirective(_HoursDirective):
    directive_type: Literal["fix_price"] = "fix_price"
    price: float = Field(..., description="Fixed price for the given hours")


class FixChargeDirective(_HoursDirective):
    directive_type: Literal["fix_charge"] = "fix_charge"
    factor: Factor


class FixDischargeDirective(_HoursDirective):
    directive_type: Literal["fix_discharge"] = "fix_discharge"
    factor: Factor


class CapChargeDirective(_HoursDirective):
    directive_type: Literal["cap_charge"] = "cap_charge"
    factor: Factor


class CapDischargeDirective(_HoursDirective):
    directive_type: Literal["cap_discharge"] = "cap_discharge"
    factor: Factor


class NoOpDirective(BaseModel):
    directive_type: Literal["no_op"] = "no_op"


Directive = Annotated[
    Union[
        FixPriceDirective,
        FixChargeDirective,
        FixDischargeDirective,
        CapChargeDirective,
        CapDischargeDirective,
        NoOpDirective,
    ],
    Field(discriminator="directive_type"),
]


class InterpretationRequest(BaseModel):
    """Payload for /interpret: 1-3 free-text notes plus hourly prices."""

    notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    prices: Annotated[list[float], Field(min_length=24, max_length=24)]


class InterpretationResponse(BaseModel):
    directives: list[Directive]


class ScheduleRequest(BaseModel):
    """Payload for /schedule: interpreted directives plus battery & tariff data."""

    directives: list[Directive]
    prices: Annotated[list[float], Field(min_length=24, max_length=24)]
    initial_soc: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    capacity_kwh: float = Field(default=10.0, gt=0)
    power_kw: float = Field(default=5.0, gt=0)
    efficiency: Annotated[float, Field(gt=0.0, le=1.0)] = 1.0


class ScheduleResponse(BaseModel):
    schedule: list[Annotated[float, Field(ge=-1.0, le=1.0)]]
    objective_cost: float
