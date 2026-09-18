"""Validate the service against the organizer's public sample cases.

Usage:
    .venv/bin/python scripts/validate_samples.py [base_url]

With no argument it starts the app in-process; with a base_url (e.g.
https://your-service.onrender.com) it tests a deployed instance instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

SAMPLES = (
    Path(__file__).resolve().parent.parent
    / "problem_statement"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)

EXPECTED_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def check_case(base_url: str, case: dict) -> tuple[bool, str]:
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{base_url}/optimize-energy", json=case["input"])
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    body = resp.json()

    # Interpretation: one entry per note, in order, valid types, applies semantics.
    interp = body.get("directive_interpretation") or []
    notes = case["input"]["operator_notes"]
    if [d.get("note_index") for d in interp] != list(range(len(notes))):
        return False, "directive_interpretation missing/out-of-order note_index entries"
    for d in interp:
        if d.get("directive_type") not in EXPECTED_TYPES:
            return False, f"unsupported directive_type {d.get('directive_type')!r}"
        if d["directive_type"] == "no_op" and (d.get("applies") or d.get("structured_adjustment")):
            return False, "no_op must have applies=false and null adjustment"
        if d["directive_type"] != "no_op" and not d.get("applies"):
            return False, f"{d['directive_type']} must have applies=true"

    # Plan + totals.
    plan = body.get("hourly_plan") or []
    if len(plan) != 24 or sorted(p["hour"] for p in plan) != list(range(24)):
        return False, "hourly_plan must contain hours 0..23 exactly once"
    total_grid = sum(p["grid_kwh"] for p in plan)
    total_cost = sum(
        p["grid_kwh"] * case["input"]["hours"][p["hour"]]["tariff_bdt_per_kwh"] for p in plan
    )
    if abs(body["total_grid_kwh"] - total_grid) > 0.01:
        return False, "total_grid_kwh mismatch"
    if abs(body["total_cost_bdt"] - total_cost) > 0.01:
        return False, "total_cost_bdt mismatch"
    if abs(body["peak_grid_kwh"] - max(p["grid_kwh"] for p in plan)) > 0.01:
        return False, "peak_grid_kwh mismatch"

    ref = sum(
        p["grid_kwh"] * case["input"]["hours"][p["hour"]]["tariff_bdt_per_kwh"]
        for p in case["expected_output"]["hourly_plan"]
    )
    ours = total_cost
    return True, f"cost {ours:.1f} BDT (reference {ref:.1f})"


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    base_url = base_url.rstrip("/")

    with httpx.Client(timeout=30.0) as client:
        health = client.get(f"{base_url}/health")
    health.raise_for_status()
    print(f"/health OK: {health.json()}")

    cases = json.loads(SAMPLES.read_text())["cases"]
    failures = 0
    for case in cases:
        ok, message = check_case(base_url, case)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {case['id']}: {message}")
        failures += 0 if ok else 1
    print(f"\n{len(cases) - failures}/{len(cases)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
