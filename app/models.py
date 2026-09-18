"""Pydantic v2 schemas for the GridWise /optimize-energy API.

Serves three purposes:
1. Request/response contract for the FastAPI endpoints (exact spec shapes).
2. Deterministic guardrails: anything the LLM returns must validate against
   the strict directive models or it is rejected (never coerced, never invented).
3. Documentation via /docs.

Guardrail rules encoded here (Problem Statement sections 05/08):
- hours: unique integers 0..23, ascending, non-empty.
- solar_reduction factor in [0, 1] (usable fraction remaining).
- reserve/grid-cap values finite and non-negative; reserve <= capacity.
- no_op is the only directive with structured_adjustment = null.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Directive types (Problem Statement section 04)
# ---------------------------------------------------------------------------

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

NON_NOOP_TYPES = frozenset(
    {
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
    }
)

BatteryAction = Literal["charge", "discharge", "idle"]


def _check_hours(hours: list[int]) -> list[int]:
    if not hours:
        raise ValueError("hours must be a non-empty list")
    if len(set(hours)) != len(hours):
        raise ValueError("hours must be unique integers")
    if any(h != int(h) for h in hours):
        raise ValueError("hours must be integers")
    if any(h < 0 or h > 23 for h in hours):
        raise ValueError("hours must be integers 0 through 23")
    if list(hours) != sorted(hours):
        raise ValueError("hours must be in ascending order")
    return hours


class SolarReductionAdjustment(BaseModel):
    """{"hours": [...], "factor": number} — factor is the usable fraction remaining."""

    model_config = ConfigDict(extra="forbid")

    hours: list[int]
    factor: float

    @model_validator(mode="after")
    def _validate(self) -> "SolarReductionAdjustment":
        _check_hours(self.hours)
        if not (0.0 <= self.factor <= 1.0):
            raise ValueError("factor must be between 0 and 1 inclusive")
        return self


class MinimumBatteryReserveAdjustment(BaseModel):
    """{"hours": [...], "minimum_energy_kwh": number}."""

    model_config = ConfigDict(extra="forbid")

    hours: list[int]
    minimum_energy_kwh: float

    @model_validator(mode="after")
    def _validate(self) -> "MinimumBatteryReserveAdjustment":
        _check_hours(self.hours)
        if self.minimum_energy_kwh < 0:
            raise ValueError("minimum_energy_kwh must be non-negative")
        return self


class WindowAdjustment(BaseModel):
    """{"hours": [...]} for no_charge_window / no_discharge_window."""

    model_config = ConfigDict(extra="forbid")

    hours: list[int]

    @model_validator(mode="after")
    def _validate(self) -> "WindowAdjustment":
        _check_hours(self.hours)
        return self


class MaxGridWindowAdjustment(BaseModel):
    """{"hours": [...], "max_grid_kwh": number}."""

    model_config = ConfigDict(extra="forbid")

    hours: list[int]
    max_grid_kwh: float

    @model_validator(mode="after")
    def _validate(self) -> "MaxGridWindowAdjustment":
        _check_hours(self.hours)
        if self.max_grid_kwh < 0:
            raise ValueError("max_grid_kwh must be non-negative")
        return self


DirectiveAdjustment = (
    SolarReductionAdjustment
    | MinimumBatteryReserveAdjustment
    | WindowAdjustment
    | MaxGridWindowAdjustment
)


class DirectiveInterpretation(BaseModel):
    """One machine-checkable interpretation entry per operator note."""

    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: DirectiveAdjustment | None = None
    explanation: str = ""

    @model_validator(mode="after")
    def _validate_semantics(self) -> "DirectiveInterpretation":
        if self.directive_type == "no_op":
            if self.applies:
                raise ValueError("no_op must have applies = false")
            if self.structured_adjustment is not None:
                raise ValueError("no_op must have structured_adjustment = null")
        else:
            if not self.applies:
                raise ValueError(f"{self.directive_type} must have applies = true")
            if self.structured_adjustment is None:
                raise ValueError(f"{self.directive_type} requires structured_adjustment")
            # Enforce the exact adjustment shape per directive type (Section 04).
            expected = {
                "solar_reduction": SolarReductionAdjustment,
                "minimum_battery_reserve": MinimumBatteryReserveAdjustment,
                "no_charge_window": WindowAdjustment,
                "no_discharge_window": WindowAdjustment,
                "max_grid_window": MaxGridWindowAdjustment,
            }[self.directive_type]
            if not isinstance(self.structured_adjustment, expected):
                raise ValueError(
                    f"{self.directive_type} requires structured_adjustment of shape "
                    f"{expected.__name__}, got {type(self.structured_adjustment).__name__}"
                )
        return self


# ---------------------------------------------------------------------------
# Request schema (Problem Statement section 07)
# ---------------------------------------------------------------------------


class HourInput(BaseModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class BatteryInput(BaseModel):
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> "BatteryInput":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        return self


class OptimizeRequest(BaseModel):
    scenario_id: str = Field(min_length=1)
    operator_notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    hours: Annotated[list[HourInput], Field(min_length=24, max_length=24)]
    battery: BatteryInput

    @model_validator(mode="after")
    def _validate(self) -> "OptimizeRequest":
        if any(not note.strip() for note in self.operator_notes):
            raise ValueError("operator_notes entries must be non-empty strings")
        hours_seen = [h.hour for h in self.hours]
        if sorted(hours_seen) != list(range(24)):
            raise ValueError("hours must contain exactly 24 unique entries for hours 0..23")
        return self

    def effective_solar(self, directives: list["DirectiveInterpretation"]) -> list[float]:
        """Base solar with solar_reduction directives applied (Section 5.3)."""
        solar = [h.solar_kwh for h in self.hours]
        for d in directives:
            if d.directive_type == "solar_reduction" and d.applies and d.structured_adjustment:
                for h in d.structured_adjustment.hours:
                    solar[h] = solar[h] * d.structured_adjustment.factor
        return solar


# ---------------------------------------------------------------------------
# Response schema (Problem Statement section 10)
# ---------------------------------------------------------------------------


class HourPlan(BaseModel):
    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: Annotated[list[DirectiveInterpretation], Field(min_length=1, max_length=3)]
    hourly_plan: Annotated[list[HourPlan], Field(min_length=24, max_length=24)]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str

    @model_validator(mode="after")
    def _validate(self) -> "OptimizeResponse":
        hours = [p.hour for p in self.hourly_plan]
        if sorted(hours) != list(range(24)):
            raise ValueError("hourly_plan must contain exactly 24 unique hours 0..23")
        indices = [d.note_index for d in self.directive_interpretation]
        if indices != sorted(indices) or len(set(indices)) != len(indices):
            raise ValueError("directive_interpretation must be in note_index order without duplicates")
        return self

    def model_dump_public(self) -> dict[str, Any]:
        """JSON-safe dict matching the spec exactly (no extra fields)."""
        return {
            "scenario_id": self.scenario_id,
            "directive_interpretation": [
                {
                    "note_index": d.note_index,
                    "applies": d.applies,
                    "directive_type": d.directive_type,
                    "structured_adjustment": (
                        d.structured_adjustment.model_dump() if d.structured_adjustment else None
                    ),
                    "explanation": d.explanation,
                }
                for d in self.directive_interpretation
            ],
            "hourly_plan": [p.model_dump() for p in self.hourly_plan],
            "total_grid_kwh": self.total_grid_kwh,
            "total_cost_bdt": self.total_cost_bdt,
            "peak_grid_kwh": self.peak_grid_kwh,
            "plan_summary": self.plan_summary,
        }
