"""GridWise API: /health + /optimize-energy (LLM -> guardrails -> optimizer).

Pipeline per request (Problem Statement section 03):
1. Pydantic request validation (400 for malformed/structurally invalid JSON).
2. LLM interprets every operator note into a structured directive proposal.
3. Deterministic guardrails validate/reconstruct each entry against the strict
   directive models; per-note LLM failures fall back to a heuristic parser so
   the service degrades gracefully instead of failing the case.
4. The optimizer applies the validated directives and produces a cost-minimal
   24-hour schedule.
5. The plan is replay-validated; totals are recalculated from the plan itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .fallback_interpreter import heuristic_interpret
from .llm import LLMError, interpret_notes
from .models import (
    DirectiveInterpretation,
    HourPlan,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    OptimizeRequest,
    OptimizeResponse,
    SolarReductionAdjustment,
    WindowAdjustment,
)
from .optimizer import OptimizerError, optimize_schedule
from .settings import settings
from .validator import validate_plan

logger = logging.getLogger("gridwise")

_ADJUSTMENT_MODELS: dict[str, type] = {
    "solar_reduction": SolarReductionAdjustment,
    "minimum_battery_reserve": MinimumBatteryReserveAdjustment,
    "no_charge_window": WindowAdjustment,
    "no_discharge_window": WindowAdjustment,
    "max_grid_window": MaxGridWindowAdjustment,
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="GridWise Directive Optimizer",
    description=(
        "LLM-assisted campus energy scheduling: operator notes are interpreted by a "
        "language model, guardrailed deterministically, then applied to an MILP that "
        "minimizes grid electricity cost over a 24-hour horizon."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)

# Response cache keyed by (scenario payload hash): repeated identical hidden
# cases (e.g. retries) skip the LLM call entirely, improving p95 latency.
_response_cache: dict[str, dict[str, Any]] = {}
_CACHE_LIMIT = 256


def _cache_key(req: OptimizeRequest) -> str:
    return hashlib.sha256(
        json.dumps(req.model_dump(), sort_keys=True, default=str).encode()
    ).hexdigest()


def _guardrail_entry(
    raw: dict[str, Any], note_index: int, note: str, battery_capacity_kwh: float
) -> DirectiveInterpretation:
    """Validate one LLM entry; reconstruct from the note text if it fails.

    The LLM output is untrusted: only entries that pass the strict pydantic
    models survive. A malformed entry degrades to the deterministic fallback
    parser for that note rather than crashing or inventing a directive.
    """
    if not isinstance(raw, dict):
        return heuristic_interpret([note], battery_capacity_kwh)[0]

    type_name = raw.get("directive_type")
    applies = bool(raw.get("applies", False))
    adj_raw = raw.get("structured_adjustment")
    explanation = str(raw.get("explanation") or "LLM interpretation.")

    if type_name == "no_op":
        if not applies:  # valid no_op
            return DirectiveInterpretation(
                note_index=note_index,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation=explanation,
            )
        return heuristic_interpret([note], battery_capacity_kwh)[0]

    model = _ADJUSTMENT_MODELS.get(str(type_name))
    if model is None or not applies:
        return heuristic_interpret([note], battery_capacity_kwh)[0]

    try:
        if not isinstance(adj_raw, dict):
            raise TypeError("structured_adjustment must be an object")
        adjustment = model.model_validate(adj_raw)
        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type=str(type_name),
            structured_adjustment=adjustment,
            explanation=explanation,
        )
    except (TypeError, ValidationError, ValueError, KeyError, AttributeError):
        logger.warning("note %d: LLM entry failed guardrails; using fallback parser", note_index)
        return heuristic_interpret([note], battery_capacity_kwh)[0]


async def _interpret_via_llm(req: OptimizeRequest) -> list[dict[str, Any]]:
    """Call the LLM through the shared lifespan HTTP client (server event loop)."""
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not configured")
    return await interpret_notes(
        notes=list(req.operator_notes),
        api_key=settings.openrouter_api_key,
        client=app.state.http_client,
        battery_capacity_kwh=req.battery.capacity_kwh,
        model=settings.openrouter_model,
        fallback_model=settings.openrouter_fallback_model,
    )


def _normalize_adjustment(
    directive: DirectiveInterpretation, note: str, capacity_kwh: float
) -> DirectiveInterpretation:
    """Deterministic time/value normalization of an LLM directive (allowed
    postprocessing per the rules). When the deterministic parser confidently
    extracts the SAME directive type from the note, its hours and numeric value
    take precedence — this corrects LLM off-by-one errors on exclusive window
    ends (e.g. "from 6 PM until 10 PM" must include hour 21). Otherwise the
    LLM output stands untouched."""
    if directive.directive_type == "no_op" or not directive.applies:
        return directive
    det = heuristic_interpret([note], capacity_kwh)[0]
    if det.directive_type != directive.directive_type or not det.applies:
        return directive
    det_adj, llm_adj = det.structured_adjustment, directive.structured_adjustment
    if det_adj is None or llm_adj is None or det_adj.hours == llm_adj.hours:
        return directive
    logger.info(
        "note %d: normalizing hours %s -> %s via deterministic parser",
        directive.note_index, llm_adj.hours, det_adj.hours,
    )
    return directive.model_copy(update={"structured_adjustment": det_adj})


async def _interpret_all_notes(req: OptimizeRequest) -> list[DirectiveInterpretation]:
    """LLM-first interpretation with deterministic guardrails and fallback."""
    capacity = req.battery.capacity_kwh
    try:
        raw_entries = await _interpret_via_llm(req)
        interpretations = [
            _guardrail_entry(raw, i, note, capacity)
            for i, (raw, note) in enumerate(zip(raw_entries, req.operator_notes))
        ]
        # Ensure exactly one entry per note (LLM may return too many/few).
        while len(interpretations) < len(req.operator_notes):
            i = len(interpretations)
            interpretations.append(heuristic_interpret([req.operator_notes[i]], capacity)[0])
        interpretations = interpretations[: len(req.operator_notes)]
        # Fallback-produced entries carry note_index 0; re-stamp to stay in order.
        for i, d in enumerate(interpretations):
            if d.note_index != i:
                interpretations[i] = d.model_copy(update={"note_index": i})
        # Deterministic normalization of time windows / numeric values.
        return [
            _normalize_adjustment(d, note, capacity)
            for d, note in zip(interpretations, req.operator_notes)
        ]
    except LLMError:
        logger.warning("LLM unavailable; using deterministic fallback interpreter")
        return heuristic_interpret(list(req.operator_notes), capacity)


async def optimize_endpoint(req: OptimizeRequest) -> OptimizeResponse:
    """Full pipeline: LLM interpretation -> guardrails -> MILP -> replay check.

    Errors map to spec status codes: 422 for a structurally valid request whose
    optimization fails, 500 for controlled internal errors (never raw traces).
    """
    key = _cache_key(req)
    cached = _response_cache.get(key)
    if cached is not None:
        return OptimizeResponse.model_validate(cached)

    try:
        directives = await _interpret_all_notes(req)
        plan_rows, relaxed = await asyncio.to_thread(optimize_schedule, req, directives)
    except OptimizerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LLMError as exc:  # should not escape (fallback handles it), belt & braces
        logger.error("LLM error escaped pipeline: %s", exc)
        directives = heuristic_interpret(list(req.operator_notes), req.battery.capacity_kwh)
        plan_rows, relaxed = await asyncio.to_thread(optimize_schedule, req, directives)

    plan = [HourPlan.model_validate(row) for row in plan_rows]
    issues = validate_plan(req, directives, plan, relaxed)
    if issues:
        logger.error("plan failed replay validation: %s", issues)
        raise HTTPException(status_code=500, detail="internal schedule validation failed")

    total_grid = round(sum(row.grid_kwh for row in plan), 6)
    tariffs = {h.hour: h.tariff_bdt_per_kwh for h in req.hours}
    total_cost = round(sum(row.grid_kwh * tariffs[row.hour] for row in plan), 6)
    peak_grid = max(row.grid_kwh for row in plan)
    response = OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=directives,
        hourly_plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=_plan_summary(directives, plan, total_cost),
    )
    if len(_response_cache) >= _CACHE_LIMIT:
        _response_cache.clear()
    _response_cache[key] = response.model_dump()
    return response


def _plan_summary(
    directives: list[DirectiveInterpretation], plan: list[HourPlan], total_cost: float
) -> str:
    applied = [d.directive_type for d in directives if d.applies]
    charges = sum(1 for p in plan if p.battery_action == "charge")
    discharges = sum(1 for p in plan if p.battery_action == "discharge")
    return (
        f"Applied {len(applied)} operator directive(s) ({', '.join(applied) or 'none'}); "
        f"battery charged in {charges} hour(s) and discharged in {discharges} hour(s) "
        f"to shift load away from expensive hours; total grid cost {total_cost:.2f} BDT."
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=None)
async def optimize_energy(request: Request) -> JSONResponse:
    """Interpret operator notes and return the optimal 24-hour schedule."""
    # Malformed JSON -> 400 (spec 6.1) before pydantic sees it.
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="malformed JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    try:
        req = OptimizeRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid request: {exc.error_count()} error(s)") from exc

    try:
        response = await optimize_endpoint(req)
    except HTTPException:
        raise
    except Exception as exc:  # controlled 500; never leak stack traces
        logger.exception("unhandled pipeline error")
        raise HTTPException(status_code=500, detail=f"internal error: {type(exc).__name__}") from exc

    return JSONResponse(status_code=200, content=response.model_dump_public())


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "controlled internal error"})
