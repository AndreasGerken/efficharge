from datetime import timedelta

import pytest

from efficharge.control.savings import (
    Interval,
    compute_savings,
    flat_profile,
    intervals_from_samples,
    per_slot,
)
from efficharge.energy.series import EnergySeries
from tests.helpers import T0, at


def test_intervals_from_samples_cap_gaps_and_end():
    samples = [(T0, 100.0), (at(minutes=1), 100.0), (at(1), 50.0)]
    ivs = intervals_from_samples(samples, end=at(1, 2))
    assert ivs[0] == Interval(T0, at(minutes=1), 100.0)
    assert ivs[1] == Interval(at(minutes=1), at(minutes=6), 100.0)  # gap capped at 5 min
    assert ivs[2] == Interval(at(1), at(1, 2), 50.0)


def test_per_slot_splits_and_shifts():
    ivs = [Interval(at(minutes=10), at(minutes=20), 600.0)]  # 0.1 kWh
    assert per_slot(ivs) == pytest.approx({T0: 0.05, at(minutes=15): 0.05})
    shifted = per_slot(ivs, shift=timedelta(minutes=5))
    assert shifted == pytest.approx({at(minutes=15): 0.1})


def test_savings_against_baseline():
    # plugged at 12:00 (price 300), charged 14:00–15:00 (price 100), 1 kWh
    prices = {at(minutes=15 * i): (300.0 if i < 8 else 100.0) for i in range(16)}
    co2 = {at(minutes=15 * i): (500.0 if i < 8 else 200.0) for i in range(16)}
    ren = {at(minutes=15 * i): (30.0 if i < 8 else 60.0) for i in range(16)}
    series = EnergySeries(price=prices, ren_share=ren, co2=co2)
    ivs = flat_profile(at(2), timedelta(hours=1), 1.0)
    result = compute_savings(ivs, actual_start=at(2), baseline_start=T0, series=series)
    assert result.kwh == pytest.approx(1.0)
    assert result.cost_actual_eur == pytest.approx(0.10)
    assert result.cost_baseline_eur == pytest.approx(0.30)
    assert result.saved_eur == pytest.approx(0.20)
    assert result.avoided_co2_g == pytest.approx(300.0)
    assert (result.ren_actual_pct, result.ren_baseline_pct) == pytest.approx((60.0, 30.0))


def test_savings_unknown_when_prices_missing():
    series = EnergySeries(price={}, ren_share={}, co2=None)
    result = compute_savings(
        flat_profile(T0, timedelta(hours=1), 1.0),
        actual_start=T0,
        baseline_start=T0,
        series=series,
    )
    assert result.saved_eur is None
    assert result.avoided_co2_g is None
