"""PuLP MILP optimizer: cost-minimal 24-hour battery schedule under directives.

One model over all 24 hours (NOT hour-by-hour greedy) so that battery-energy
continuity and the end-of-day neutrality rule are enforced globally:

    variables per hour h:  c[h] (charge kW), d[h] (discharge kW), soc[h] (kWh),
                           z[h] (binary mode: 0 = charge allowed, 1 = discharge)
    continuity:            soc[h] == soc[h-1] + efficiency * c[h] - d[h]
    bounds:                0 <= soc[h] <= capacity, 0 <= c[h], d[h] <= power_kw
    mode exclusivity:      c[h] <= power_kw * (1 - z[h]);  d[h] <= power_kw * z[h]
    neutrality (hard):     soc[23] == initial stored energy
    directives:            fix_charge / fix_discharge pin c/d on given hours;
                           cap_charge / cap_discharge limit them;
                           fix_price overrides the market price for trading
                           those hours (placeholder reading -- align with spec).

The binaries prevent simultaneous charge+discharge in the same hour, which a
pure LP permits (degenerate optima like c=d=2.5 kW). CBC solves this in
milliseconds. Infeasible models (e.g. contradictory fix directives) raise
OptimizerError -> the API reports a safe failure instead of inventing output.
"""

from __future__ import annotations

import pulp

from .models import (
    CapChargeDirective,
    CapDischargeDirective,
    Directive,
    FixChargeDirective,
    FixDischargeDirective,
    FixPriceDirective,
    ScheduleRequest,
)

HOURS = range(24)


class OptimizerError(RuntimeError):
    """Raised when the schedule LP is infeasible or the solver fails."""


def optimize_schedule(req: ScheduleRequest, directives: list[Directive]) -> tuple[list[float], float]:
    """Solve the 24-hour LP. Returns (normalized schedule, objective cost)."""
    prob = pulp.LpProblem("battery_schedule", pulp.LpMinimize)

    charge = {h: pulp.LpVariable(f"c_{h}", lowBound=0, upBound=req.power_kw) for h in HOURS}
    discharge = {h: pulp.LpVariable(f"d_{h}", lowBound=0, upBound=req.power_kw) for h in HOURS}
    soc = {h: pulp.LpVariable(f"soc_{h}", lowBound=0, upBound=req.capacity_kwh) for h in HOURS}
    mode = {h: pulp.LpVariable(f"z_{h}", cat=pulp.LpBinary) for h in HOURS}

    initial_energy = req.initial_soc * req.capacity_kwh

    # Battery energy continuity across all 24 hours.
    for h in HOURS:
        prev = soc[h - 1] if h > 0 else initial_energy
        prob += soc[h] == prev + req.efficiency * charge[h] - discharge[h], f"continuity_{h}"
        # No simultaneous charge & discharge in the same hour.
        prob += charge[h] <= req.power_kw * (1 - mode[h]), f"mode_c_{h}"
        prob += discharge[h] <= req.power_kw * mode[h], f"mode_d_{h}"

    # Hard end-of-day neutrality: finish with what we started with.
    prob += soc[23] == initial_energy, "end_of_day_neutrality"

    # Effective price per hour (directives may override the market tariff).
    price = {h: float(req.prices[h]) for h in HOURS}
    fixed_charge: dict[int, float] = {}
    fixed_discharge: dict[int, float] = {}

    for d in directives:
        if isinstance(d, FixPriceDirective):
            for h in d.hours:
                price[h] = d.price
        elif isinstance(d, FixChargeDirective):
            for h in d.hours:
                fixed_charge[h] = d.factor * req.power_kw
        elif isinstance(d, FixDischargeDirective):
            for h in d.hours:
                fixed_discharge[h] = d.factor * req.power_kw
        elif isinstance(d, CapChargeDirective):
            for h in d.hours:
                prob += charge[h] <= d.factor * req.power_kw, f"cap_c_{h}"
        elif isinstance(d, CapDischargeDirective):
            for h in d.hours:
                prob += discharge[h] <= d.factor * req.power_kw, f"cap_d_{h}"
        # NoOpDirective: nothing to constrain.

    for h, kw in fixed_charge.items():
        prob += charge[h] == kw, f"fix_c_{h}"
    for h, kw in fixed_discharge.items():
        prob += discharge[h] == kw, f"fix_d_{h}"

    # Minimize net energy cost: buy at price when charging, earn when discharging.
    prob += pulp.lpSum(price[h] * (charge[h] - discharge[h]) for h in HOURS)

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise OptimizerError(f"schedule LP is {pulp.LpStatus[status]} (contradictory directives?)")

    schedule = [
        round((charge[h].value() or 0.0) - (discharge[h].value() or 0.0), 6) / req.power_kw
        for h in HOURS
    ]
    return schedule, round(pulp.value(prob.objective) or 0.0, 6)
