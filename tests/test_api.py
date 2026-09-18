"""API tests via FastAPI TestClient with a mocked LLM (no network)."""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.optimizer import OptimizerError

HOURS = [
    {"hour": h, "demand_kwh": 100.0, "solar_kwh": 10.0, "tariff_bdt_per_kwh": 8.0}
    for h in range(24)
]
BATTERY = {
    "capacity_kwh": 200.0,
    "initial_energy_kwh": 100.0,
    "minimum_energy_kwh": 40.0,
    "max_charge_kwh_per_hour": 50.0,
    "max_discharge_kwh_per_hour": 50.0,
}

RAW_LLM_OUTPUT = [
    {
        "note_index": 0,
        "applies": True,
        "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [10, 11], "factor": 0.5},
        "explanation": "Panel cleaning reduces solar output.",
    },
    {
        "note_index": 1,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Unrelated note.",
    },
]


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        app._response_cache = getattr(app, "_response_cache", {})
        # Isolate cache between tests.
        from app import main as main_module

        main_module._response_cache.clear()
        yield test_client
        main_module._response_cache.clear()


def _payload(**overrides):
    payload = {
        "scenario_id": "API-1",
        "operator_notes": [
            "Solar will drop to 50% from 10 AM to noon.",
            "The cafeteria menu changes tomorrow.",
        ],
        "hours": HOURS,
        "battery": BATTERY,
    }
    payload.update(overrides)
    return payload


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_optimize_success_schema(client):
    with patch("app.main._interpret_via_llm", return_value=RAW_LLM_OUTPUT):
        resp = client.post("/optimize-energy", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario_id"] == "API-1"
    assert [d["note_index"] for d in body["directive_interpretation"]] == [0, 1]
    assert body["directive_interpretation"][0]["directive_type"] == "solar_reduction"
    assert body["directive_interpretation"][1]["directive_type"] == "no_op"
    assert body["directive_interpretation"][1]["applies"] is False
    assert body["directive_interpretation"][1]["structured_adjustment"] is None
    assert len(body["hourly_plan"]) == 24
    assert sorted(p["hour"] for p in body["hourly_plan"]) == list(range(24))
    # Totals must match recalculation from the plan.
    total_grid = sum(p["grid_kwh"] for p in body["hourly_plan"])
    total_cost = sum(
        p["grid_kwh"] * HOURS[p["hour"]]["tariff_bdt_per_kwh"] for p in body["hourly_plan"]
    )
    assert body["total_grid_kwh"] == pytest.approx(total_grid, abs=0.01)
    assert body["total_cost_bdt"] == pytest.approx(total_cost, abs=0.01)
    assert body["peak_grid_kwh"] == pytest.approx(max(p["grid_kwh"] for p in body["hourly_plan"]), abs=0.01)
    # Solar reduced to 50% at hours 10-11 must be respected in the plan.
    for p in body["hourly_plan"]:
        if p["hour"] in (10, 11):
            assert p["solar_used_kwh"] <= 5.0 + 0.01


def test_malformed_json_is_400(client):
    resp = client.post(
        "/optimize-energy", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400


def test_non_object_body_is_400(client):
    resp = client.post("/optimize-energy", json=[1, 2, 3])
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("scenario_id"),
        lambda p: p.update(operator_notes=[]),
        lambda p: p.update(operator_notes=["a", "b", "c", "d"]),
        lambda p: p.update(hours=HOURS[:23]),
        lambda p: p.update(battery={**BATTERY, "capacity_kwh": -5}),
        lambda p: p.update(operator_notes=["ok", ""]),
    ],
)
def test_structurally_invalid_request_is_400(client, mutate):
    payload = _payload()
    mutate(payload)
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_llm_failure_falls_back_and_still_200(client):
    from app.llm import LLMError

    with patch("app.main._interpret_via_llm", side_effect=LLMError("provider down")):
        resp = client.post("/optimize-energy", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    # Fallback should still interpret the solar note deterministically.
    assert body["directive_interpretation"][0]["directive_type"] == "solar_reduction"


def test_garbage_llm_entries_degrade_to_fallback(client):
    garbage = [
        {"directive_type": "make_money_fast", "hours": [1]},
        "not even a dict",
    ]
    with patch("app.main._interpret_via_llm", return_value=garbage):
        resp = client.post("/optimize-energy", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert [d["note_index"] for d in body["directive_interpretation"]] == [0, 1]
    # Garbage -> safe no_op from the fallback parser (menu note is a true no_op).
    assert all(d["directive_type"] in ("no_op", "solar_reduction") for d in body["directive_interpretation"])


def test_cache_hit_skips_llm(client):
    with patch("app.main._interpret_via_llm", return_value=RAW_LLM_OUTPUT) as mock_llm:
        first = client.post("/optimize-energy", json=_payload())
        second = client.post("/optimize-energy", json=_payload())
    assert first.status_code == second.status_code == 200
    assert mock_llm.call_count == 1  # second identical request served from cache


def test_optimizer_error_is_422(client):
    with (
        patch("app.main._interpret_via_llm", return_value=RAW_LLM_OUTPUT),
        patch("app.main.optimize_schedule", side_effect=OptimizerError("no way")),
    ):
        resp = client.post("/optimize-energy", json=_payload())
    assert resp.status_code == 422


def test_unexpected_error_is_controlled_500(client):
    with (
        patch("app.main._interpret_via_llm", return_value=RAW_LLM_OUTPUT),
        patch("app.main.optimize_schedule", side_effect=RuntimeError("boom-secret-value")),
    ):
        resp = client.post("/optimize-energy", json=_payload())
    assert resp.status_code == 500
    # The raw exception text must not leak to the client.
    assert "boom-secret-value" not in resp.text


def test_end_to_end_with_real_pipeline_no_llm_key(client):
    """No API key configured: fallback path must still produce a valid plan."""
    resp = client.post("/optimize-energy", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["hourly_plan"]) == 24
