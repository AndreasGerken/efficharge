import math
from datetime import timedelta

import pytest

from efficharge.control.remaining import CutoffModel, estimate, learn
from tests.helpers import T0, at


def charge(start, plateau_min: int, decline_min: int, tau_min=40.0, watts=180.0, cutoff=20.0):
    """Plateau, then an exponential decline until the charger switches off at ``cutoff``."""
    samples = [(start + timedelta(minutes=m), watts) for m in range(plateau_min)]
    for m in range(decline_min):
        w = 0.95 * watts * math.exp(-m / tau_min)
        if w < cutoff:
            break
        samples.append((start + timedelta(minutes=plateau_min + m), w))
    end = samples[-1][0] + timedelta(minutes=1)
    samples.append((end + timedelta(minutes=1), 0.8))  # charger idling afterwards
    return samples, end


def test_learns_the_switch_off_power_also_from_an_almost_full_battery():
    full, end1 = charge(T0, 120, 200, cutoff=20.0)
    # plugged in almost full: no plateau, 30 W falling to the switch-off (like 5 Nov)
    almost, end2 = charge(at(24), 0, 30, tau_min=10, watts=31.0, cutoff=19.0)
    model = learn([(full, end1), (almost, end2)])
    assert 19.0 <= model.cutoff_w() <= 20.5


def test_on_the_plateau_planned_duration_minus_charged_time():
    samples, _ = charge(at(24), 100, 100)
    running = samples[:30]  # 29 min in, still full power
    result = estimate(running, running[-1][0], None, started=at(24), planned=timedelta(hours=3))
    assert result.kind == "plateau"
    assert result.left == timedelta(hours=3) - timedelta(minutes=29)


def test_in_the_decline_extrapolated_to_the_learned_switch_off_power():
    samples, _ = charge(T0, 10, 200, tau_min=40)
    running = samples[: 10 + 45]  # 45 min into the decline: 0.95·180·e^(-44/40) ≈ 57 W
    now = running[-1][0]
    model = CutoffModel((20.0,))
    result = estimate(running, now, model, started=T0, planned=timedelta(hours=3))
    assert result.kind == "decline"
    # 40 · ln(57/20) ≈ 42 min
    assert result.left.total_seconds() == pytest.approx(42 * 60, abs=2 * 60)
    # without history: 10 % of the plateau power (18 W) → a bit longer
    result = estimate(running, now, None, started=T0, planned=timedelta(hours=3))
    assert result.left.total_seconds() == pytest.approx(46 * 60, abs=2 * 60)


def test_below_the_switch_off_power_means_any_moment_and_overdue_plateau_too():
    samples, _ = charge(T0, 10, 200, tau_min=40)
    running = samples[: 10 + 45]
    late = estimate(running, running[-1][0], CutoffModel((60.0,)), started=T0, planned=None)
    assert late.left == timedelta(0)
    flat, _ = charge(T0, 300, 10)
    overdue = estimate(flat[:200], at(4), None, started=T0, planned=timedelta(hours=3))
    assert overdue.kind == "plateau" and overdue.left == timedelta(0)
    assert estimate(flat[:2], at(1), None, started=T0, planned=None) is None


def test_nothing_learned_without_readings():
    assert learn([([], at(1))]) is None
