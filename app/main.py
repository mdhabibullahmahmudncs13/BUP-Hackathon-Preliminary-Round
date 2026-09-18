"""Two-endpoint microservice: /interpret (LLM) and /schedule (LP optimizer)."""

from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from .llm import LLMError, interpret_notes
from .models import InterpretationRequest, InterpretationResponse, ScheduleRequest, ScheduleResponse
from .optimizer import OptimizerError, optimize_schedule
from .settings import settings

app = FastAPI(
    title="Directive Optimizer",
    description=(
        "Interprets free-text operator notes into battery directives (LLM), then "
        "computes a cost-minimal 24-hour schedule (LP). Invalid LLM output and "
        "infeasible schedules fail safely with explicit errors."
    ),
    version="0.1.0",
)

# One shared async client for the OpenRouter calls (connection pooling).
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/interpret", response_model=InterpretationResponse)
async def interpret(req: InterpretationRequest) -> InterpretationResponse:
    """Map 1-3 free-text notes to validated directives via the LLM."""
    if not settings.openrouter_api_key:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured")
    try:
        return await interpret_notes(
            notes=req.notes,
            api_key=settings.openrouter_api_key,
            client=get_http_client(),
            model=settings.openrouter_model,
            fallback_model=settings.openrouter_fallback_model,
        )
    except LLMError as exc:
        # Safe failure: report the guardrail rejection instead of inventing output.
        raise HTTPException(status_code=422, detail=f"guardrail rejection: {exc}") from exc


@app.post("/schedule", response_model=ScheduleResponse)
async def schedule(req: ScheduleRequest) -> ScheduleResponse:
    """Compute the cost-minimal 24-hour schedule for the given directives."""
    try:
        sched, cost = optimize_schedule(req, list(req.directives))
    except OptimizerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ScheduleResponse(schedule=sched, objective_cost=cost)
