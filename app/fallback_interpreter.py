"""Deterministic heuristic fallback interpreter.

The LLM is the primary interpretation path (mandatory per the rubric). This
module is the controlled-degradation path used only when the LLM/provider
fails or returns unusable output for a note, so the service still answers
instead of crashing. It parses the common phrasings of the five supported
directives plus no_op. Distractor notes fall through to no_op.
"""

from __future__ import annotations

import re

from .models import (
    DirectiveInterpretation,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    SolarReductionAdjustment,
    WindowAdjustment,
)

# Hour mentions: "1 PM", "10 am", explicit 24h "13:00" (colon form),
# or the words "noon"/"midnight". Bare numbers ("20%", "90 kWh") never match.
_TIME_RE = re.compile(
    r"\b(?:(\d{1,2})\s*(am|pm)|(\d{1,2}):(00|30)|(noon|midnight))\b",
    re.IGNORECASE,
)

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

_KWH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kwh|kw\b)", re.IGNORECASE)

_SPelled_FACTOR = {"half": 0.5, "one-fifth": 0.2, "one fifth": 0.2, "a fifth": 0.2, "quarter": 0.25, "one-quarter": 0.25}


def _to_24h(hour: int, suffix: str | None) -> int:
    suffix = (suffix or "").lower()
    if suffix == "pm":
        return 12 if hour == 12 else (hour + 12 if hour < 12 else hour)
    if suffix == "am":
        return 0 if hour == 12 else hour
    return hour  # 24h form already


def _time_mentions(text: str) -> list[int]:
    """All hour mentions in order: '1 PM' -> 13, '13:00' -> 13, 'noon' -> 12."""
    hours: list[int] = []
    for m in _TIME_RE.finditer(text):
        if m.group(1) is not None:
            hours.append(_to_24h(int(m.group(1)), m.group(2)))
        elif m.group(3) is not None:
            h = int(m.group(3))
            if 0 <= h <= 23:
                hours.append(h)
        else:
            word = (m.group(5) or "").lower()
            hours.append(12 if word == "noon" else 0)
    return hours


def _hours_from_window(text: str) -> list[int] | None:
    """Extract a start-inclusive / end-exclusive whole-hour window."""
    mentions = _time_mentions(text)
    if not mentions:
        return None
    start = mentions[0]
    end = mentions[1] if len(mentions) > 1 else None
    if end is None:
        return [start]
    if end <= start:  # wraps midnight ("10 PM until 2 AM") -> end on next day
        end += 24
    # end-exclusive: 1 PM to 3 PM -> [13, 14]
    return sorted({h % 24 for h in range(start, end)})


def _first_kwh(text: str) -> float | None:
    m = _KWH_RE.search(text)
    return float(m.group(1)) if m else None


def _try_adjustment(model_cls, **kwargs):
    """Build an adjustment or None if the values are malformed."""
    try:
        return model_cls(**kwargs)
    except Exception:  # noqa: BLE001 - fail-safe: any malformed value -> None
        return None


def _no_op(note_index: int, reason: str) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index, applies=False, directive_type="no_op", structured_adjustment=None,
        explanation=reason,
    )


def _fallback_interpret_one(note: str, battery_capacity_kwh: float) -> DirectiveInterpretation:
    lower = note.lower()

    def hours() -> list[int] | None:
        return _hours_from_window(lower)

    # --- battery reserve: "keep at least X kWh" / "at least Y% of capacity" ---
    if re.search(r"reserve|keep at least|at least \d|remain|stored in the battery|hold", lower) and re.search(
        r"battery", lower
    ):
        pct = _NUM_RE.search(lower)
        kwh = _first_kwh(lower)
        minimum: float | None = None
        if pct and "capacity" in lower:
            minimum = float(pct.group(1)) / 100.0 * battery_capacity_kwh
        elif kwh is not None:
            minimum = kwh
        window = hours()
        adj = (
            _try_adjustment(MinimumBatteryReserveAdjustment, hours=window, minimum_energy_kwh=minimum)
            if minimum is not None and window
            else None
        )
        if adj is not None:
            return DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type="minimum_battery_reserve",
                structured_adjustment=adj,
                explanation="Heuristic fallback: battery reserve for the stated window.",
            )
        return _no_op(0, "Reserve note could not be parsed deterministically (fallback).")

    # --- solar_reduction: "80% reduction", "drop to 20%", "half of normal" ---
    if re.search(r"solar|pv|panel|photovoltaic", lower) and not re.search(
        r"battery|charge|discharge|grid|reserve", lower
    ):
        percentages = [float(p) for p in _NUM_RE.findall(lower)]
        factor: float | None = None
        if percentages:
            p = percentages[0] / 100.0
            if re.search(r"reduc|curtail|below normal|less than normal", lower):
                # "an 80% reduction" -> 0.2 usable
                factor = 1.0 - p
            elif re.search(
                r"drop|fall|down to|treated as|leave|of (the )?(normal|forecast|usual|output)",
                lower,
            ):
                # "drop to 20%" / "treated as 25% of the forecast" -> usable fraction
                factor = p
        if factor is None:
            for word, f in _SPelled_FACTOR.items():
                if word in lower and re.search(r"of (the )?(normal|forecast|usual|output)", lower):
                    factor = f
                    break
        window = hours()
        adj = (
            _try_adjustment(SolarReductionAdjustment, hours=window, factor=round(factor, 4))
            if factor is not None and window
            else None
        )
        if adj is not None:
            return DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type="solar_reduction",
                structured_adjustment=adj,
                explanation="Heuristic fallback: solar availability reduced for the stated window.",
            )
        return _no_op(0, "Solar note could not be parsed deterministically (fallback).")

    # --- no_charge_window / no_discharge_window ---
    negation = re.search(
        r"(do not|don't|must not|not be|no |disable|disabled|unavailable|isolated|stopped|cannot|can't|out of service)",
        lower,
    )
    window = hours()
    if negation and "discharge" not in lower and re.search(r"charg", lower):
        adj = _try_adjustment(WindowAdjustment, hours=window) if window else None
        if adj is not None:
            return DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type="no_charge_window",
                structured_adjustment=adj,
                explanation="Heuristic fallback: charging unavailable in the stated window.",
            )
        return _no_op(0, "Charging note could not be parsed deterministically (fallback).")
    if negation and re.search(r"discharg", lower):
        adj = _try_adjustment(WindowAdjustment, hours=window) if window else None
        if adj is not None:
            return DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type="no_discharge_window",
                structured_adjustment=adj,
                explanation="Heuristic fallback: discharging unavailable in the stated window.",
            )
        return _no_op(0, "Discharge note could not be parsed deterministically (fallback).")

    # --- max_grid_window: "grid import must not exceed X" ---
    if re.search(r"grid", lower) and re.search(r"(exceed|at or below|below|limit|cap|max)", lower):
        kwh = _first_kwh(lower)
        window = hours()
        adj = (
            _try_adjustment(MaxGridWindowAdjustment, hours=window, max_grid_kwh=kwh)
            if kwh is not None and window
            else None
        )
        if adj is not None:
            return DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type="max_grid_window",
                structured_adjustment=adj,
                explanation="Heuristic fallback: grid import capped in the stated window.",
            )
        return _no_op(0, "Grid-cap note could not be parsed deterministically (fallback).")

    # --- distractor ---
    return _no_op(0, "Note does not affect the schedule.")


def heuristic_interpret(notes: list[str], battery_capacity_kwh: float) -> list[DirectiveInterpretation]:
    """Deterministic last-resort interpretation; never raises."""
    results: list[DirectiveInterpretation] = []
    for i, note in enumerate(notes):
        try:
            d = _fallback_interpret_one(note, battery_capacity_kwh)
        except Exception:  # noqa: BLE001 - absolute last resort: a note must never crash the service
            d = _no_op(i, "Interpretation failed; treating the note as a no-op.")
        results.append(
            DirectiveInterpretation(
                note_index=i,
                applies=d.applies,
                directive_type=d.directive_type,
                structured_adjustment=d.structured_adjustment,
                explanation=d.explanation,
            )
        )
    return results
