from datetime import UTC, datetime, timedelta

import pytest

from efficharge.energy.series import SLOT, ceil_slot, expand_steps, floor_slot, from_unix
from tests.helpers import T0, at, make_series


def test_floor_and_ceil_slot():
    assert floor_slot(at(minutes=14)) == T0
    assert ceil_slot(at(minutes=1)) == at(minutes=15)
    assert ceil_slot(T0) == T0


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        floor_slot(datetime(2026, 1, 1))  # noqa: DTZ001


def test_hourly_data_expands_to_four_equal_quarter_hours():
    out = expand_steps({T0: 100.0, at(1): 200.0, at(2): 300.0})
    assert [out[at(minutes=15 * i)] for i in range(12)] == [100.0] * 4 + [200.0] * 4 + [300.0] * 4
    assert at(3) not in out


def test_mixed_hourly_then_quarter_hour_history():
    out = expand_steps({T0: 1.0, at(1): 2.0, at(1, 15): 3.0, at(1, 30): 4.0})
    assert [out.get(at(minutes=15 * i)) for i in range(8)] == [1.0] * 4 + [2.0, 3.0, 4.0, None]


def test_quarter_hour_data_is_kept():
    out = expand_steps({T0: 1.0, at(minutes=15): 2.0, at(minutes=30): 3.0})
    assert out == {T0: 1.0, at(minutes=15): 2.0, at(minutes=30): 3.0}


def test_none_values_and_long_gaps_become_gaps():
    out = expand_steps({T0: 1.0, at(minutes=15): None, at(minutes=30): 3.0, at(5): 4.0})
    assert at(minutes=15) not in out
    assert at(minutes=45) not in out  # 15-min native resolution, not stretched to 5 h
    assert out[at(5)] == 4.0


def test_from_unix():
    ts = int(T0.timestamp())
    assert from_unix([ts], [1.5]) == {T0: 1.5}
    assert next(iter(from_unix([ts], [1.5]))).tzinfo == UTC


def test_average_is_time_weighted_and_does_not_interpolate():
    s = make_series([100.0, 200.0])
    # 10 min of 100 and 5 min of 200 → (1000 + 1000) / 15
    assert s.average(s.price, at(minutes=5), at(minutes=20)) == pytest.approx(2000 / 15)
    assert s.average(s.price, at(1), at(2)) is None


def test_known_until_stops_at_first_gap():
    s = make_series([1.0, 2.0, None, 4.0], ren=[50.0, None, 50.0, 50.0])
    assert s.known_until(need_price=True, need_ren=False, after=T0) == at(minutes=30)
    assert s.known_until(need_price=True, need_ren=True, after=T0) == at(minutes=15)
    # mid-slot start
    assert s.known_until(need_price=True, need_ren=False, after=at(minutes=7)) == at(minutes=30)


def test_weighted_sum():
    s = make_series([100.0, 200.0])
    assert s.weighted_sum(s.price, {T0: 1.0, T0 + SLOT: 0.5}) == 200.0
    assert s.weighted_sum(s.price, {at(1): 1.0}) is None
    assert s.weighted_sum(s.price, {at(1): 0.0}) == 0.0


def test_dst_day_has_100_quarter_hours():
    # 25 Oct 2026: clocks go back in Europe/Berlin. The UTC grid needs no special case.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 25, tzinfo=tz).astimezone(UTC)
    end = datetime(2026, 10, 26, tzinfo=tz).astimezone(UTC)
    s = make_series([1.0] * 200, start=start)
    assert len(list(s.slots(start, end))) == 100
    assert (end - start) == timedelta(hours=25)
