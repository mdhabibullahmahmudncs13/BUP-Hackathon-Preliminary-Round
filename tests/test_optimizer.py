"""Optimizer tests: LP correctness, directive effects, and infeasibility."""

import pytest

from app.models import (
    CapDischargeDirective,
    FixChargeDirective,
    FixDischargeDirective,
    ScheduleRequest,
)
from app.optimizer import OptimizerError, optimize_schedule

PRICES = [50.0] * 24


def make_req(directives, prices=None, **kwargs):
    payload = {
        "directives": directives,
        "prices": prices or PRICES,
        "capacity_kwh": 10.0,
        "power_kw": 5.0,
    }
    payload.update(kwargs)
    return ScheduleRequest.model_validate(payload)


def test_no_directives_flat_prices_zero_cost():
    req = make_req([])
    sched, cost = optimize_schedule(req, [])
    assert abs(cost) < 1e-6
    assert all(abs(v) < 1e-6 for v in sched)  # flat prices -> do nothing


def test_arbitrage_charges_cheap_discharges_expensive():
    prices = [10.0] * 12 + [90.0] * 12
    req = make_req([], prices=prices)
    sched, cost = optimize_schedule(req, [])
    assert any(v > 0.9 for v in sched[:12])  # charge during cheap hours
    assert any(v < -0.9 for v in sched[12:])  # discharge during expensive hours
    assert cost < 0  # net revenue


def test_end_of_day_neutrality():
    """soc[23] == start is enforced by the LP; with efficiency 1.0 this means
    total net energy over the day must be zero."""
    prices = [10.0] * 12 + [90.0] * 12
    req = make_req([], prices=prices)
    sched, _ = optimize_schedule(req, [])
    net_energy = sum(v * req.power_kw for v in sched)
    assert abs(net_energy) < 1e-3


def test_fix_charge_pins_hours():
    d = FixChargeDirective(hours=[5, 6], factor=0.5)
    req = make_req([d])
    sched, _ = optimize_schedule(req, [d])
    assert sched[5] == pytest.approx(0.5)
    assert sched[6] == pytest.approx(0.5)


def test_cap_discharge_limits_hours():
    """Hour 18 is strictly the most expensive hour, so its discharge cap is
    fully used; the rest of the energy discharges in the cheaper 90-hours."""
    d = CapDischargeDirective(hours=[18], factor=0.2)
    prices = [10.0] * 12 + [90.0] * 6 + [120.0] + [90.0] * 5
    assert prices[18] == 120.0 and len(prices) == 24
    req = make_req([d], prices=prices)
    sched, _ = optimize_schedule(req, [d])
    assert sched[18] == pytest.approx(-0.2, abs=1e-6)  # cap fully used, discharge is negative


def test_contradictory_fixes_raise():
    # Full-power charge AND discharge in the same hour -> infeasible LP.
    fc = FixChargeDirective(hours=[10], factor=1.0)
    fd = FixDischargeDirective(hours=[10], factor=1.0)
    req = make_req([fc, fd])
    with pytest.raises(OptimizerError):
        optimize_schedule(req, [fc, fd])
