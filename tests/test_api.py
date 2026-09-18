"""API-level tests via FastAPI TestClient."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_schedule_endpoint_minimal():
    resp = client.post(
        "/schedule",
        json={"directives": [], "prices": [10.0] * 12 + [90.0] * 12},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["schedule"]) == 24
    assert body["objective_cost"] < 0


def test_schedule_endpoint_infeasible_is_422():
    # Simultaneous full charge + discharge in one hour -> infeasible.
    resp = client.post(
        "/schedule",
        json={
            "directives": [
                {"directive_type": "fix_charge", "hours": [10], "factor": 1.0},
                {"directive_type": "fix_discharge", "hours": [10], "factor": 1.0},
            ],
            "prices": [10.0] * 24,
        },
    )
    assert resp.status_code == 422


def test_interpret_without_api_key_is_503():
    resp = client.post(
        "/interpret",
        json={"notes": ["charge at night"], "prices": [10.0] * 24},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_interpret_malformed_llm_output_is_422():
    """LLM returns invalid directives -> guardrail rejection, not coercion."""
    from app import settings as settings_module

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"directives": [ junk'}}]}

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=FakeResp())

    settings_module.settings.openrouter_api_key = "test-key"
    with patch("app.main.get_http_client", return_value=fake_client):
        resp = client.post(
            "/interpret",
            json={"notes": ["charge at night"], "prices": [10.0] * 24},
        )
    assert resp.status_code == 422
    assert "guardrail rejection" in resp.json()["detail"]
