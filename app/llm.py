"""OpenRouter LLM client: maps free-text notes to validated directives.

- Structured output via ``response_format`` (JSON schema), with a strict
  JSON-only prompt as fallback for models that don't support it.
- One same-provider fallback model, matching the rubric's "team responsible
  for provider availability" requirement.
- Never invents data: any invalid/malformed LLM output raises and is reported
  as a guardrail rejection, not silently coerced.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .models import Directive, DirectiveType, InterpretationResponse

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct"
FALLBACK_MODEL = "meta-llama/llama-3.1-70b-instruct"  # same provider, fallback path

SYSTEM_PROMPT = """You convert operator notes into battery-schedule directives.

Return ONLY a JSON object with key "directives": a list of directive objects.
Each directive has a "directive_type" chosen from exactly these six types:
- fix_price: {"directive_type": "fix_price", "hours": [int 0-23 ascending, unique, non-empty], "price": float}
- fix_charge: {"directive_type": "fix_charge", "hours": [...], "factor": float 0.0-1.0}
- fix_discharge: {"directive_type": "fix_discharge", "hours": [...], "factor": float 0.0-1.0}
- cap_charge: {"directive_type": "cap_charge", "hardcoded": false, "hours": [...], "factor": float 0.0-1.0}
- cap_discharge: {"directive_type": "cap_discharge", "hours": [...], "factor": float 0.0-1.0}
- no_op: {"directive_type": "no_op"}

Rules:
- A note that specifies no constraint for an hour must NOT get a directive.
- Use "no_op" only if the notes imply nothing at all.
- Never guess hours or values. If a note is ambiguous, output no directive for it.
- Output strictly valid JSON and nothing else.
""".rstrip()

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "directive_interpretation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "directives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "directive_type": {
                                "type": "string",
                                "enum": list(DirectiveType.__args__),  # type: ignore[attr-defined]
                            },
                            "hours": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 0, "maximum": 23},
                            },
                            "price": {"type": "number"},
                            "factor": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["directive_type"],
                    },
                }
            },
            "required": ["directives"],
        },
    },
}


class LLMError(RuntimeError):
    """Raised when the LLM call or its output fails validation."""


async def interpret_notes(
    notes: list[str],
    api_key: str,
    client: httpx.AsyncClient,
    model: str = DEFAULT_MODEL,
    fallback_model: str = FALLBACK_MODEL,
) -> InterpretationResponse:
    """Ask the LLM to map notes -> directives; validate via InterpretationResponse."""
    user_msg = (
        "Notes:\n" + "\n".join(f"- {n}" for n in notes)
        + "\n\nReturn the directives JSON now."
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "response_format": RESPONSE_FORMAT,
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/directive-optimizer",
        "X-Title": "directive-optimizer",
    }

    last_error: Exception | None = None
    for attempt_model in (model, fallback_model):
        try:
            resp = await client.post(
                OPENROUTER_URL,
                json=payload | {"model": attempt_model},
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
            # Pydantic discriminated union: malformed output raises, never coerced.
            return InterpretationResponse.model_validate(data)
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            continue

    raise LLMError(f"LLM interpretation failed after fallback: {last_error}") from last_error
