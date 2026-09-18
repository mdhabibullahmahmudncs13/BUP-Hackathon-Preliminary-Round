"""Public sample-case harness.

Replays the organizer's public sample pack through the full pipeline with the
deterministic fallback interpreter (no network), then validates every response
exactly like the judge: interpretation shape, directive application, energy
balance, battery rules, end-of-day neutrality, and recalculated totals.

Also compares our interpretation against the sample pack's ground truth and
our recalculated cost against the reference optimal cost.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.llm import LLMError
from app.main import app
from app.validator import validate_plan

SAMPLES = (
    Path(__file__).resolve().parent.parent
    / "problem_statement"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)

CASES = json.loads(SAMPLES.read_text())["cases"] if SAMPLES.exists() else []


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        from app import main as main_module

        main_module._response_cache.clear()
        # Deterministic harness: force the fallback interpreter so tests never
        # depend on network/LLM availability or cost. The live-LLM path is
        # exercised separately (tests/test_live_llm.py, skipped by default).
        with patch("app.main._interpret_via_llm", side_effect=LLMError("offline test")):
            yield test_client
        main_module._response_cache.clear()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_public_sample_case(client, case):
    payload = case["input"]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # --- top-level schema ---
    assert body["scenario_id"] == payload["scenario_id"]
    for key in (
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    ):
        assert key in body, f"missing {key}"
    assert len(body["hourly_plan"]) == 24
    assert body["plan_summary"].strip()

    # --- interpretation: one entry per note, in order ---
    notes = payload["operator_notes"]
    interp = body["directive_interpretation"]
    assert [d["note_index"] for d in interp] == list(range(len(notes)))
    for d in interp:
        assert d["directive_type"] in {
            "solar_reduction",
            "minimum_battery_reserve",
            "no_charge_window",
            "no_discharge_window",
            "max_grid_window",
            "no_op",
        }
        if d["directive_type"] == "no_op":
            assert d["applies"] is False
            assert d["structured_adjustment"] is None
        else:
            assert d["applies"] is True
            assert isinstance(d["structured_adjustment"], dict)
            assert d["structured_adjustment"]["hours"], "hours must be non-empty"

    # --- compare against the sample pack's ground truth ---
    ground_truth = case["expected_output"]["directive_interpretation"]
    ours = {d["note_index"]: d for d in interp}
    for gt in ground_truth:
        got = ours[gt["note_index"]]
        assert got["directive_type"] == gt["directive_type"], (
            f"{case['id']} note {gt['note_index']}: expected {gt['directive_type']}, "
            f"got {got['directive_type']}"
        )
        if gt["structured_adjustment"] is not None:
            got_adj = got["structured_adjustment"]
            gt_adj = gt["structured_adjustment"]
            assert got_adj["hours"] == gt_adj["hours"], (
                f"{case['id']} note {gt['note_index']}: hours {got_adj['hours']} != {gt_adj['hours']}"
            )
            for key in gt_adj:
                if key == "hours":
                    continue
                assert got_adj[key] == pytest.approx(gt_adj[key], abs=0.01), (
                    f"{case['id']} note {gt['note_index']}: {key} mismatch"
                )

    # --- judge-style plan replay (directives from OUR interpretation) ---
    from app.models import DirectiveInterpretation, HourPlan, OptimizeRequest

    req = OptimizeRequest.model_validate(payload)
    directives = [DirectiveInterpretation.model_validate(d) for d in interp]
    plan = [HourPlan.model_validate(p) for p in body["hourly_plan"]]
    issues = validate_plan(req, directives, plan)
    assert issues == [], f"{case['id']}: {issues}"

    # --- totals recalculated from the plan ---
    total_grid = sum(p.grid_kwh for p in plan)
    total_cost = sum(p.grid_kwh * req.hours[p.hour].tariff_bdt_per_kwh for p in plan)
    assert body["total_grid_kwh"] == pytest.approx(total_grid, abs=0.01)
    assert body["total_cost_bdt"] == pytest.approx(total_cost, abs=0.01)
    assert body["peak_grid_kwh"] == pytest.approx(max(p.grid_kwh for p in plan), abs=0.01)

    # --- cost sanity: never worse than 5% above the reference optimal ---
    # Our cost is recalculated from our plan; the reference used its own plan.
    ref_recalc = sum(
        p["grid_kwh"] * payload["hours"][p["hour"]]["tariff_bdt_per_kwh"]
        for p in case["expected_output"]["hourly_plan"]
    )
    assert total_cost <= ref_recalc * 1.05 + 1.0, (
        f"{case['id']}: our cost {total_cost:.2f} vs reference {ref_recalc:.2f}"
    )


def test_sample_pack_loaded():
    assert len(CASES) == 10, "public sample pack should contain 10 cases"
