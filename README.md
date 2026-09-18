# GridWise — LLM-Assisted Campus Energy Optimization

**BUP CSE FEST 2026 · Hackathon · Online Preliminary**

One HTTP service with two endpoints:

1. **`POST /optimize-energy`** — receives a 24-hour campus energy scenario plus 1–3
   natural-language operator notes, interprets the notes with an **LLM**, converts them
   into validated structured directives, applies them to a **MILP optimizer**, and returns
   the machine-checkable interpretation plus the cost-minimal 24-hour schedule.
2. **`GET /health`** — readiness probe returning `{"status": "ok"}`.

## Architecture

```
operator notes ──▶ LLM (OpenRouter, structured JSON output)
                      │  untrusted structured data
                      ▼
              deterministic guardrails (pydantic: type whitelist, hours 0-23
              ascending & unique, factor/reserve/cap ranges, applies semantics,
              exact adjustment shapes; malformed output -> deterministic
              heuristic fallback, never invented directives)
                      │  validated directives
                      ▼
              MILP optimizer (PuLP + CBC): energy balance, effective solar,
              battery continuity/bounds/rates, end-of-day neutrality,
              solar_reduction / minimum_battery_reserve / no_charge_window /
              no_discharge_window / max_grid_window
                      │  24-hour plan
                      ▼
              replay validator (judge-identical checks) + recalculated totals
                      │
                      ▼
              POST /optimize-energy response (interpretation + schedule)
```

- **LLM role**: `meta-llama/llama-3.3-70b-instruct` via OpenRouter (configurable),
  with same-provider fallback `meta-llama/llama-3.1-70b-instruct`. The model is part
  of the interpretation path that produces the optimization constraints (mandated by
  the problem statement).
- **Deterministic postprocessing**: time windows from the LLM are normalized by an
  independent parser (end-exclusive convention), and any malformed LLM entry is
  replaced by the heuristic interpreter's output for that note — the service never
  crashes or invents an unsupported directive type.
- **Optimizer/solver**: mixed-integer linear program solved with CBC via PuLP.
  One global 24-hour model (not greedy) so battery continuity and end-of-day
  neutrality hold exactly. If an interpretation is provably infeasible, directives
  are progressively relaxed so a valid GridWise plan is still returned.

## Local quickstart (clean environment)

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd <repo-dir>

# 2. Create a virtualenv (Python 3.12+) and install
python -m venv .venv
source .venv/bin/activate
pip install .

# 3. Configure the LLM provider
cp .env.example .env
# edit .env and set OPENROUTER_API_KEY=sk-or-v1-...

# 4. Start the service
uvicorn app.main:app --host 0.0.0.0 --port 8000
# (or: python -m app)

# 5. Health check
curl http://localhost:8000/health
# -> {"status":"ok"}

# 6. Run one public sample case against the live service
.venv/bin/python scripts/validate_samples.py http://localhost:8000
# -> 10/10 cases passed
```

Without `OPENROUTER_API_KEY` the service still works end to end using the
deterministic heuristic interpreter (clearly flagged in the response explanations),
so the optimizer and API can be tested without any provider credentials.

## Environment variables

| Name | Required | Default | Purpose |
| --- | --- | --- | --- |
| `OPENROUTER_API_KEY` | yes (for LLM path) | — | OpenRouter API key; service runs without it via the deterministic fallback |
| `OPENROUTER_MODEL` | no | `meta-llama/llama-3.3-70b-instruct` | Primary interpretation model |
| `OPENROUTER_FALLBACK_MODEL` | no | `meta-llama/llama-3.1-70b-instruct` | Same-provider fallback model |

Secrets are loaded from `.env` (gitignored). No keys are committed, logged, or
returned in responses; stack traces are never exposed to clients.

## API examples

Health:

```bash
curl http://localhost:8000/health
```

Optimize (abridged scenario):

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "GRID-101",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
      {"hour": 23, "demand_kwh": 200, "solar_kwh": 0, "tariff_bdt_per_kwh": 9}
    ],
    "battery": {
      "capacity_kwh": 500, "initial_energy_kwh": 200, "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100, "max_discharge_kwh_per_hour": 100
    }
  }'
```

Response (abridged):

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {
      "note_index": 0, "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "Solar availability is reduced during panel cleaning."
    },
    {
      "note_index": 1, "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's energy schedule."
    }
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 180.0, "solar_used_kwh": 0.0, "battery_action": "idle",
     "battery_kwh": 0.0, "battery_energy_after_kwh": 200.0}
  ],
  "total_grid_kwh": 0.0, "total_cost_bdt": 0.0, "peak_grid_kwh": 0.0,
  "plan_summary": "Applied 1 operator directive(s) (solar_reduction); ..."
}
```

Error codes: `400` malformed/structurally invalid JSON, `422` valid JSON whose
optimization is infeasible, `500` controlled internal error (no stack traces).
Malformed LLM output never surfaces as an error — it degrades to the deterministic
fallback interpreter per note.

## Testing

```bash
pip install .[dev]
pytest                       # 80+ tests: guardrails, MILP, validator, API, sample pack
RUN_LIVE_LLM=1 pytest tests/test_live_llm.py   # optional: real OpenRouter round-trip
.venv/bin/python scripts/validate_samples.py   # public pack against a running service
```

`tests/test_samples.py` replays all 10 organizer public cases through the full API
with the deterministic interpreter and validates them judge-style (interpretation
ground truth, directive application, energy balance, battery rules, neutrality,
recalculated totals, cost vs reference). All 10 cases match the reference optimal
cost exactly.

## Docker

```bash
docker build -t gridwise-optimizer .
docker run --rm -p 8000:8000 -e OPENROUTER_API_KEY=sk-or-v1-... gridwise-optimizer
curl http://localhost:8000/health
```

The image binds `0.0.0.0:8000`, runs as a non-root user, and contains no baked-in
secrets. Publish it to Docker Hub/GHCR with an exact tag as the fallback execution
path for organizers.

## Deployment

Any platform that can run the container or `uvicorn` works (Render, Railway, Fly.io,
a VPS). Requirements: publicly reachable base URL, `/health` ready within 60s of
start, `POST /optimize-energy` completing well under the 30s timeout (the MILP
solves in milliseconds; latency is dominated by the LLM round-trip, typically 1–3s),
and no authentication on the judging path.

## Dependencies & credits

- [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn — HTTP API
- [Pydantic v2](https://docs.pydantic.dev/) — schemas and deterministic guardrails
- [PuLP](https://github.com/coin-or/pulp) (CBC solver) — MILP optimization
- [httpx](https://www.python-httpx.org/) — OpenRouter API client
- [OpenRouter](https://openrouter.ai/) — LLM provider (Meta Llama 3.3/3.1 70B)
- AI coding assistance was used for implementation; the architecture
  (LLM → guardrails → MILP → replay validation) and all logic are the team's work.

## Known limitations

- The heuristic fallback covers the common phrasings of the five directive types;
  heavily paraphrased notes may degrade to `no_op` when the LLM is unavailable.
- The infeasibility-relaxation ladder prefers returning a valid plan over failing
  when the interpreted directives are contradictory (organizer ground truth is
  guaranteed feasible, so this only guards against our own interpretation errors).
- Battery round-trip efficiency is 1.0 (as specified — no loss model is defined in
  the problem statement).
