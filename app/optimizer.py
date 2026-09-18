"""GridWise MILP optimizer: cost-minimal 24-hour schedule under directives.

Formulation (Problem Statement sections 05/09) per hour h in 0..23:

    variables:  g[h] grid kWh, s[h] solar used kWh, c[h] charge kWh,
                d[h] discharge kWh, e[h] battery energy after hour h, z[h] binary
    balance:    g[h] + s[h] + d[h] == demand[h] + c[h]
    solar:      0 <= s[h] <= effective_solar[h]   (after solar_reduction)
    rates:      0 <= c[h] <= max_charge, 0 <= d[h] <= max_discharge
    continuity: e[h] == e[h-1] + c[h] - d[h],  e[-1] = initial_energy
    bounds:     reserve_lb[h] <= e[h] <= capacity  (reserve_lb = max(base min,
                minimum_battery_reserve directives active at h))
    neutrality: e[23] == initial_energy
    windows:    c[h] = 0 (no_charge_window), d[h] = 0 (no_discharge_window)
    grid cap:   g[h] <= max_grid_kwh (max_grid_window)
    exclusivity: c[h] <= max_charge * (1 - z[h]);  d[h] <= max_discharge * z[h]
    objective:  minimize SUM tariff[h] * g[h]

Robustness ladder: if the strict model is infeasible (only possible when our
interpretation differs from the organizer's feasible ground truth), directives
are progressively relaxed group by group so the service still returns a valid
GridWise plan instead of failing the whole case. The base problem (no
directives) is always feasible: grid alone can serve demand with the battery
idle, and initial energy within [min, capacity] keeps neutrality trivially
satisfied — both are enforced by the request schema.

Post-processing replays snapped values so the emitted plan is internally exact:
solar-first dispatch, grid = net load - solar used, battery chain recomputed.
"""

from __future__ import annotations

from typing import Any

import pulp

from .models import DirectiveInterpretation, OptimizeRequest

HOURS = range(24)

# Slack groups for the infeasibility ladder (see module docstring).
_RELAX_SEQUENCE: list[frozenset[str]] = [
    frozenset(),  # strict
    frozenset({"reserve"}),
    frozenset({"grid"}),
    frozenset({"windows"}),
    frozenset({"reserve", "grid"}),
    frozenset({"reserve", "grid", "windows"}),
    frozenset({"all"}),
]

_SNAP = 1e-6  # values below this are treated as zero
_ROUND = 6  # decimals for emitted plan values


class OptimizerError(RuntimeError):
    """Raised only if every relaxation level fails (practically unreachable)."""


def _reserve_lower_bounds(
    req: OptimizeRequest, directives: list[DirectiveInterpretation]
) -> list[float]:
    """Per-hour minimum battery energy: base reserve plus reserve directives."""
    lb = [req.battery.minimum_energy_kwh] * 24
    for d in directives:
        if d.directive_type == "minimum_battery_reserve" and d.applies and d.structured_adjustment:
            for h in d.structured_adjustment.hours:
                lb[h] = max(lb[h], d.structured_adjustment.minimum_energy_kwh)
    return lb


def _window_hours(directives: list[DirectiveInterpretation], kind: str) -> set[int]:
    hours: set[int] = set()
    for d in directives:
        if d.directive_type == kind and d.applies and d.structured_adjustment:
            hours.update(d.structured_adjustment.hours)
    return hours


def _grid_caps(directives: list[DirectiveInterpretation]) -> dict[int, float]:
    caps: dict[int, float] = {}
    for d in directives:
        if d.directive_type == "max_grid_window" and d.applies and d.structured_adjustment:
            for h in d.structured_adjustment.hours:
                caps[h] = min(caps.get(h, float("inf")), d.structured_adjustment.max_grid_kwh)
    return caps


def _solve_once(
    req: OptimizeRequest,
    directives: list[DirectiveInterpretation],
    relax: frozenset[str],
) -> list[dict[str, Any]] | None:
    """Build and solve one MILP variant. Returns raw hourly values or None."""
    solar = req.effective_solar(directives)
    demand = [h.demand_kwh for h in req.hours]
    tariff = [h.tariff_bdt_per_kwh for h in req.hours]
    bat = req.battery

    charge_ban = set() if "windows" in relax or "all" in relax else _window_hours(directives, "no_charge_window")
    discharge_ban = (
        set() if "windows" in relax or "all" in relax else _window_hours(directives, "no_discharge_window")
    )
    grid_caps = {} if "grid" in relax or "all" in relax else _grid_caps(directives)
    reserve_lb = (
        [bat.minimum_energy_kwh] * 24
        if "reserve" in relax or "all" in relax
        else _reserve_lower_bounds(req, directives)
    )
    if "all" in relax:
        directives = []

    prob = pulp.LpProblem("gridwise_schedule", pulp.LpMinimize)
    grid = {h: pulp.LpVariable(f"g_{h}", lowBound=0) for h in HOURS}
    used = {h: pulp.LpVariable(f"s_{h}", lowBound=0, upBound=max(0.0, solar[h])) for h in HOURS}
    charge = {h: pulp.LpVariable(f"c_{h}", lowBound=0, upBound=bat.max_charge_kwh_per_hour) for h in HOURS}
    discharge = {
        h: pulp.LpVariable(f"d_{h}", lowBound=0, upBound=bat.max_discharge_kwh_per_hour) for h in HOURS
    }
    energy = {h: pulp.LpVariable(f"e_{h}", lowBound=reserve_lb[h], upBound=bat.capacity_kwh) for h in HOURS}
    mode = {h: pulp.LpVariable(f"z_{h}", cat=pulp.LpBinary) for h in HOURS}

    for h in HOURS:
        prev = energy[h - 1] if h > 0 else bat.initial_energy_kwh
        prob += energy[h] == prev + charge[h] - discharge[h], f"continuity_{h}"
        prob += grid[h] + used[h] + discharge[h] == demand[h] + charge[h], f"balance_{h}"
        # One battery action per hour (clean battery_action labeling).
        prob += charge[h] <= bat.max_charge_kwh_per_hour * (1 - mode[h]), f"mode_c_{h}"
        prob += discharge[h] <= bat.max_discharge_kwh_per_hour * mode[h], f"mode_d_{h}"
        if h in charge_ban:
            prob += charge[h] == 0, f"no_charge_{h}"
        if h in discharge_ban:
            prob += discharge[h] == 0, f"no_discharge_{h}"
        if h in grid_caps:
            prob += grid[h] <= grid_caps[h], f"grid_cap_{h}"

    prob += energy[23] == bat.initial_energy_kwh, "end_of_day_neutrality"
    prob += pulp.lpSum(tariff[h] * grid[h] for h in HOURS), "total_grid_cost"

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        return None

    return [
        {
            "grid": max(0.0, grid[h].value() or 0.0),
            "solar_used": max(0.0, used[h].value() or 0.0),
            "charge": max(0.0, charge[h].value() or 0.0),
            "discharge": max(0.0, discharge[h].value() or 0.0),
        }
        for h in HOURS
    ]


def _snap(value: float) -> float:
    return 0.0 if abs(value) < _SNAP else round(value, _ROUND)


def _replay_plan(req: OptimizeRequest, directives: list[DirectiveInterpretation], raw: list[dict]) -> list[dict[str, Any]]:
    """Deterministic post-processing: snap values, recompute the battery chain
    and dispatch so the emitted plan satisfies balance/solar/bounds exactly."""
    solar = req.effective_solar(directives)
    bat = req.battery
    plan: list[dict[str, Any]] = []
    level = bat.initial_energy_kwh

    for h in HOURS:
        c = _snap(raw[h]["charge"])
        d = _snap(raw[h]["discharge"])
        # Solar-first dispatch on the net load keeps balance exact by construction.
        net = max(0.0, req.hours[h].demand_kwh + c - d)
        s = _snap(min(solar[h], net))
        g = _snap(max(0.0, net - s))
        level = level + c - d
        if c > 0:
            action, magnitude = "charge", c
        elif d > 0:
            action, magnitude = "discharge", d
        else:
            action, magnitude = "idle", 0.0
        plan.append(
            {
                "hour": h,
                "grid_kwh": g,
                "solar_used_kwh": s,
                "battery_action": action,
                "battery_kwh": magnitude,
                "battery_energy_after_kwh": round(level, _ROUND),
            }
        )
    return plan


def optimize_schedule(
    req: OptimizeRequest, directives: list[DirectiveInterpretation]
) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """Solve the schedule MILP with the robustness ladder.

    Returns (hourly_plan rows, relaxation set used). Raises OptimizerError only
    if every relaxation level fails, which the request schema makes unreachable.
    """
    last_error: str = "no attempt made"
    for relax in _RELAX_SEQUENCE:
        try:
            raw = _solve_once(req, directives, relax)
        except pulp.PulpSolverError as exc:  # solver crashed -> try next level
            last_error = f"solver error: {exc}"
            continue
        if raw is None:
            last_error = f"infeasible at relaxation {sorted(relax) or 'strict'}"
            continue
        return _replay_plan(req, directives, raw), relax
    raise OptimizerError(f"schedule optimization failed: {last_error}")
