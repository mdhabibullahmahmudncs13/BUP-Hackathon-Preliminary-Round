"""OpenRouter LLM client: maps operator notes to directive interpretations.

- Structured output via ``response_format`` (JSON schema) with a strict
  JSON-only prompt fallback for models that don't support it.
- One same-provider fallback model (rubric: team owns provider availability).
- Output is treated as untrusted data: the pipeline re-validates every entry
  against the pydantic guardrail models before it reaches the optimizer.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct"
FALLBACK_MODEL = "meta-llama/llama-3.1-70b-instruct"  # same provider, fallback path

SYSTEM_PROMPT = """You interpret campus operator notes for a 24-hour energy scheduler.
Hours are indexed 0..23 (hour 0 = midnight, 13 = 1 PM). Time windows are
start-inclusive and END-EXCLUSIVE: "from 1 PM to 3 PM", "between 1 PM and 3 PM",
"from 13:00 until 15:00" all mean hours [13, 14]. "from 6 PM until 9 PM" means
[18, 19, 20]. "until 10 PM" or "to 10 PM" means the window ENDS at 10 PM, so
the last included hour is 9 PM: [18, 19, 20, 21]. NEVER include the end hour.

For EACH note, return exactly one object with these keys:
- "note_index": the zero-based index of the note.
- "applies": true if the note changes the energy schedule, false otherwise.
- "directive_type": one of "solar_reduction", "minimum_battery_reserve",
  "no_charge_window", "no_discharge_window", "max_grid_window", "no_op".
- "structured_adjustment": for the matching type, else null.
- "explanation": one short sentence.

Directive shapes (use EXACTLY these keys):
- solar_reduction: {"hours": [ints], "factor": <usable fraction remaining, 0..1>}
  An "80% reduction" means factor 0.2; "drop to 20%" also means 0.2;
  "half of normal output" means 0.5. Percentages of OUTPUT REMAINING are the factor directly.
- minimum_battery_reserve: {"hours": [ints], "minimum_energy_kwh": <number>}
  If the note states a percent of battery capacity, multiply by the given capacity_kwh.
- no_charge_window: {"hours": [ints]}  (charging unavailable: "do not charge", "charger isolated/unavailable", "charging disabled")
- no_discharge_window: {"hours": [ints]} (discharging unavailable: "must not discharge", "discharge disabled")
- max_grid_window: {"hours": [ints], "max_grid_kwh": <number>} (grid import cap: "grid import must not exceed X kWh", "grid intake at or below X")
- no_op: null  (notes about cafeterias, menus, bookings, meetings, deadlines, sports, libraries, next week/month, or anything unrelated to energy)

Rules:
- hours must be unique integers 0..23 in ascending order, non-empty.
- factor is the fraction REMAINING, never the reduction percentage.
- Never invent directive types, hours, or numbers that are not in the note.
- Notes about future/other days still count if they constrain this 24-hour schedule.
- Return ONLY a JSON object: {"interpretations": [ ...one object per note, in order... ]}"""

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "directive_interpretations",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "interpretations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "note_index": {"type": "integer"},
                            "applies": {"type": "boolean"},
                            "directive_type": {
                                "type": "string",
                                "enum": [
                                    "solar_reduction",
                                    "minimum_battery_reserve",
                                    "no_charge_window",
                                    "no_discharge_window",
                                    "max_grid_window",
                                    "no_op",
                                ],
                            },
                            "structured_adjustment": {
                                "type": ["object", "null"],
                                "additionalProperties": {"type": ["number", "array", "string", "boolean", "null"]},
                            },
                            "explanation": {"type": "string"},
                        },
                        "required": [
                            "note_index",
                            "applies",
                            "directive_type",
                            "structured_adjustment",
                            "explanation",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["interpretations"],
            "additionalProperties": False,
        },
    },
}


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unusable output."""


def _extract_content(body: dict[str, Any]) -> str:
    """Pull the assistant text out of an OpenRouter chat completion body."""
    message = body["choices"][0]["message"]
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):  # some providers return content parts
        parts = [p.get("text", "") for p in content if isinstance(p, dict)]
        text = "".join(parts).strip()
        if text:
            return text
    raise KeyError("message.content")


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the JSON object from model text, tolerating code fences or prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("LLM JSON was not an object")
    return data


def _build_messages(notes: list[str], battery_capacity_kwh: float) -> list[dict[str, str]]:
    notes_block = "\n".join(f"[{i}] {note}" for i, note in enumerate(notes))
    user_msg = (
        f"Battery capacity: {battery_capacity_kwh} kWh.\n"
        f"Operator notes ({len(notes)}):\n{notes_block}\n\n"
        "Return the interpretations JSON object now, one entry per note in note_index order."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


async def interpret_notes(
    notes: list[str],
    api_key: str,
    client: httpx.AsyncClient,
    battery_capacity_kwh: float,
    model: str = DEFAULT_MODEL,
    fallback_model: str = FALLBACK_MODEL,
) -> list[dict[str, Any]]:
    """Call the LLM and return the raw interpretations list.

    Raises LLMError on any failure; output is NOT trusted until re-validated
    by the pipeline's guardrails.
    """
    messages = _build_messages(notes, battery_capacity_kwh)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/gridwise-directive-optimizer",
        "X-Title": "gridwise-directive-optimizer",
    }

    last_error: Exception | None = None
    for attempt_model in (model, fallback_model):
        payload: dict[str, Any] = {
            "model": attempt_model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": RESPONSE_FORMAT,
        }
        try:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers, timeout=25.0)
            resp.raise_for_status()
            content = _extract_content(resp.json())
            data = _extract_json(content)
            interp = data.get("interpretations")
            if not isinstance(interp, list) or not interp:
                raise ValueError("missing 'interpretations' array")
            return interp
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
            last_error = exc
            continue

    raise LLMError(f"LLM interpretation failed after fallback: {last_error}") from last_error
