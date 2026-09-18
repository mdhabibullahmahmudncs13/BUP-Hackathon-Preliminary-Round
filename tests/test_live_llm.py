"""Optional live-LLM test: hits the real OpenRouter API.

Skipped unless RUN_LIVE_LLM=1 is set (keeps `pytest` hermetic and free).
Run manually before submission to verify the LLM path end to end:

    RUN_LIVE_LLM=1 .venv/bin/python -m pytest tests/test_live_llm.py -q
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1 to run live LLM tests"
)

CASES = json.loads(
    (
        Path(__file__).resolve().parent.parent
        / "problem_statement"
        / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
    ).read_text()
)["cases"]


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        from app import main as main_module

        main_module._response_cache.clear()
        yield test_client
        main_module._response_cache.clear()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_live_llm_matches_ground_truth(client, case):
    """Full pipeline with the real LLM: interpretation must match ground truth."""
    resp = client.post("/optimize-energy", json=case["input"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    ground_truth = case["expected_output"]["directive_interpretation"]
    ours = {d["note_index"]: d for d in body["directive_interpretation"]}
    for gt in ground_truth:
        got = ours[gt["note_index"]]
        assert got["directive_type"] == gt["directive_type"], (
            f"{case['id']} note {gt['note_index']}: expected {gt['directive_type']}, "
            f"got {got['directive_type']} ({got['explanation']})"
        )
        if gt["structured_adjustment"] is not None:
            got_adj, gt_adj = got["structured_adjustment"], gt["structured_adjustment"]
            assert got_adj["hours"] == gt_adj["hours"], (
                f"{case['id']} note {gt['note_index']}: hours {got_adj['hours']} != {gt_adj['hours']}"
            )
            for key in gt_adj:
                if key != "hours":
                    assert got_adj[key] == pytest.approx(gt_adj[key], abs=0.01)
