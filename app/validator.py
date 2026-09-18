"""Independent replay validation of a final hourly plan.

Mirrors how the judge verifies schedules (Problem Statement section 11.3):
every hour must satisfy the energy-balance equation, solar usage limits,
battery bounds and rate limits, directive-specific constraints, and the
end-of-day neutrality rule. Totals must match recalculation.

Used to self-check the optimizer output before responding and in the test
suite. Returns a list of violation strings; empty means valid.
"""

from __future__ import annotations

import math

from .models import DirectiveInterpretation, HourPlan, OptimizeRequest

TOL = 0.01  # judge tolerance: 0.01 kWh / 0.01 BDT absolute


def _enforced_directives(
    directives: list[DirectiveInterpretation], relax: frozenset[str] | None
) -> list[DirectiveInterpretation]:
    """Filter out directives the optimizer had to relax (impossible constraints).

    Mirrors optimizer._RELAX_SEQUENCE semantics so validation only checks what
    was actually enforced during optimization.
    """
    if not relax:
        return directives
    if "all" in relax:
        return []
    kept: list[DirectiveInterpretation] = []
    for d in directives:
        t = d.directive_type
        if t == "no_op" or t == "minimum_battery_reserve" and "reserve" not in relax or t == "max_grid_window" and "grid" not in relax or t in ("no_charge_window", "no_discharge_window") and "windows" not in relax:
            kept.append(d)
    return kept


def validate_plan(
    req: OptimizeRequest,
    directives: list[DirectiveInterpretation],
    plan: list[HourPlan],
    relax: frozenset[str] | None = None,
) -> list[str]:
    """Replay the plan hour by hour; return all violations found."""
    issues: list[str] = []
    directives = _enforced_directives(directives, relax)
    solar = req.effective_solar(directives)
    bat = req.battery

    for d in directives:
        if d.directive_type != "no_op" and not d.applies:
            issues.append(f"note {d.note_index}: non-no_op directive with applies=false")

    for row in plan:
        h = row.hour
        hour_in = req.hours[h]
        if any(
            not math.isfinite(v)
            for v in (row.grid_kwh, row.solar_used_kwh, row.battery_kwh, row.battery_energy_after_kwh)
        ):
            issues.append(f"hour {h}: non-finite value")
            continue

        # Energy balance: grid + solar_used + discharge == demand + charge.
        discharge = row.battery_kwh if row.battery_action == "discharge" else 0.0
        charge = row.battery_kwh if row.battery_action == "charge" else 0.0
        lhs = row.grid_kwh + row.solar_used_kwh + discharge
        rhs = hour_in.demand_kwh + charge
        if abs(lhs - rhs) > TOL:
            issues.append(
                f"hour {h}: energy balance {lhs:.3f} != demand+charge {rhs:.3f}"
            )

        # Solar usage cannot exceed effective solar.
        if row.solar_used_kwh > solar[h] + TOL:
            issues.append(f"hour {h}: solar_used {row.solar_used_kwh:.3f} > effective {solar[h]:.3f}")

        # Rate limits and idle consistency.
        if row.battery_action == "idle" and row.battery_kwh > TOL:
            issues.append(f"hour {h}: idle with battery_kwh {row.battery_kwh:.3f}")
        if row.battery_action == "charge" and row.battery_kwh > bat.max_charge_kwh_per_hour + TOL:
            issues.append(f"hour {h}: charge {row.battery_kwh:.3f} > rate {bat.max_charge_kwh_per_hour:.3f}")
        if row.battery_action == "discharge" and row.battery_kwh > bat.max_discharge_kwh_per_hour + TOL:
            issues.append(
                f"hour {h}: discharge {row.battery_kwh:.3f} > rate {bat.max_discharge_kwh_per_hour:.3f}"
            )

        # Directive-specific checks.
        for d in directives:
            adj = d.structured_adjustment
            if not (d.applies and adj):
                continue
            if h not in adj.hours:
                continue
            if d.directive_type == "no_charge_window" and row.battery_action == "charge":
                issues.append(f"hour {h}: charge inside no_charge_window")
            if d.directive_type == "no_discharge_window" and row.battery_action == "discharge":
                issues.append(f"hour {h}: discharge inside no_discharge_window")
            if d.directive_type == "max_grid_window" and row.grid_kwh > adj.max_grid_kwh + TOL:
                issues.append(f"hour {h}: grid {row.grid_kwh:.3f} > cap {adj.max_grid_kwh:.3f}")

    # Battery continuity + bounds (sequential; reserve active from hour start).
    reserve_lb = [bat.minimum_energy_kwh] * 24
    for d in directives:
        adj = d.structured_adjustment
        if d.directive_type == "minimum_battery_reserve" and d.applies and adj:
            for h in adj.hours:
                reserve_lb[h] = max(reserve_lb[h], adj.minimum_energy_kwh)

    level = bat.initial_energy_kwh
    for row in plan:
        h = row.hour
        delta = 0.0
        if row.battery_action == "charge":
            delta = row.battery_kwh
        elif row.battery_action == "discharge":
            delta = -row.battery_kwh
        level += delta
        if abs(level - row.battery_energy_after_kwh) > TOL:
            issues.append(f"hour {h}: battery chain break {level:.3f} vs reported {row.battery_energy_after_kwh:.3f}")
        if level < reserve_lb[h] - TOL:
            issues.append(f"hour {h}: battery {level:.3f} below minimum {reserve_lb[h]:.3f}")
        if level > bat.capacity_kwh + TOL:
            issues.append(f"hour {h}: battery {level:.3f} above capacity {bat.capacity_kwh:.3f}")
        level = row.battery_energy_after_kwh  # continue from reported (snapped) value

    # End-of-day neutrality.
    if plan and abs(plan[-1].battery_energy_after_kwh - bat.initial_energy_kwh) > TOL:
        issues.append(
            f"end-of-day battery {plan[-1].battery_energy_after_kwh:.3f} != initial {bat.initial_energy_kwh:.3f}"
        )

    return issues
