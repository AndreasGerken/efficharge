import itertools
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from efficharge.planner.deadlines import WORKDAYS, DeadlineRule, resolve_deadline
from efficharge.planner.duration import estimate_duration
from efficharge.planner.window import (
    PlanReason,
    Priority,
    Weights,
    best_window,
    candidate_starts,
)
from tests.helpers import T0, at, make_series

BERLIN = ZoneInfo("Europe/Berlin")
H1 = timedelta(hours=1)
MONEY = Weights(price=1, eco=0)
ECO = Weights(price=0, eco=1)


# ── window search ─────────────────────────────────────────────────────────────


def test_finds_cheapest_one_hour_window():
    prices = [50] * 8 + [10] * 4 + [50] * 8  # cheap 14:00–15:00
    plan = best_window(make_series(prices), now=T0, deadline=at(5), duration=H1, weights=MONEY)
    assert plan.start == at(2)
    assert plan.end == at(3)
    assert plan.reason is PlanReason.OPTIMAL
    assert plan.chosen.avg_price == 10
    assert plan.baseline.start == T0
    assert plan.baseline.avg_price == 50


def test_step_function_no_spline_overshoot():
    # A spline through [100, 0, 100] would go below 0 between points.
    prices = [100.0] * 4 + [0.0] * 4 + [100.0] * 4
    plan = best_window(make_series(prices), now=T0, deadline=at(3), duration=H1, weights=MONEY)
    assert plan.chosen.avg_price == 0.0


def test_window_must_end_before_deadline():
    prices = [50] * 8 + [10] * 4
    plan = best_window(make_series(prices), now=T0, deadline=at(2, 30), duration=H1, weights=MONEY)
    assert plan.end <= at(2, 30)
    assert plan.start == at(1, 30)


def test_eco_mode_uses_renewable_share():
    prices = [10] * 4 + [50] * 4
    ren = [20] * 4 + [90] * 4
    s = make_series(prices, ren)
    assert best_window(s, now=T0, deadline=at(2), duration=H1, weights=ECO).start == at(1)
    assert best_window(s, now=T0, deadline=at(2), duration=H1, weights=MONEY).start == T0


def test_priorities_agree_flag():
    s = make_series([50] * 4 + [10] * 4, [20] * 4 + [90] * 4)  # cheap = green
    plan = best_window(s, now=T0, deadline=at(2), duration=H1, weights=MONEY)
    assert plan.priorities_agree
    s = make_series([10] * 4 + [50] * 4, [20] * 4 + [90] * 4)  # cheap = dirty
    plan = best_window(s, now=T0, deadline=at(2), duration=H1, weights=MONEY)
    assert not plan.priorities_agree
    assert plan.eco_choice.start == at(1)
    assert plan.money_choice.start == T0


def test_nearly_identical_windows_count_as_agreeing():
    # cheapest starts at 13:00, greenest at 13:15: same window for practical purposes
    prices = [50] * 4 + [10] * 4 + [50] * 8
    ren = [20] * 5 + [90] * 4 + [20] * 7
    plan = best_window(make_series(prices, ren), now=T0, deadline=at(4), duration=H1, weights=MONEY)
    assert plan.eco_choice.start != plan.money_choice.start
    assert plan.priorities_agree


def test_now_off_grid_is_a_candidate():
    now = at(minutes=7)
    plan = best_window(make_series([1] * 12), now=now, deadline=at(3), duration=H1, weights=MONEY)
    assert plan.start == now  # all equal → earliest
    assert plan.reason is PlanReason.NOW_IS_BEST
    assert candidate_starts(now, at(minutes=30))[:3] == [now, at(minutes=15), at(minutes=30)]


def test_deadline_too_close_starts_now():
    plan = best_window(make_series([1] * 8), now=T0, deadline=at(0, 30), duration=H1, weights=MONEY)
    assert plan.start == T0
    assert plan.reason is PlanReason.DEADLINE_TOO_CLOSE


def test_no_data_starts_now_instead_of_crashing():
    # the first prototype crashed here in np.argmin([])
    plan = best_window(make_series([]), now=T0, deadline=at(10), duration=H1, weights=MONEY)
    assert plan.start == T0
    assert plan.reason is PlanReason.NO_DATA


def test_deadline_beyond_known_prices_uses_known_part():
    prices = [50] * 4 + [10] * 4  # data ends at 14:00, deadline 20:00
    plan = best_window(make_series(prices), now=T0, deadline=at(8), duration=H1, weights=MONEY)
    assert plan.start == at(1)
    assert plan.reason is PlanReason.PARTIAL_DATA


def test_window_not_multiple_of_slot():
    prices = [50] * 4 + [10] * 6 + [50] * 4
    d = timedelta(minutes=80)
    plan = best_window(make_series(prices), now=T0, deadline=at(3, 30), duration=d, weights=MONEY)
    assert plan.end - plan.start == d
    assert plan.start == at(1)


def test_price_only_mode_ignores_missing_ren_share():
    s = make_series([50] * 4 + [10] * 4, ren=[None] * 8)
    plan = best_window(s, now=T0, deadline=at(2), duration=H1, weights=MONEY)
    assert plan.start == at(1)
    assert plan.eco_choice is None  # cannot compare without renewables data


def test_weights_validation_and_presets():
    with pytest.raises(ValueError):
        Weights(0, 0)
    assert Weights.from_priority(Priority.ECO).eco > Weights.from_priority(Priority.ECO).price
    assert Weights.from_slider(1.5) == Weights(1.0, 0.0)


def test_eco_first_still_breaks_ties_by_price():
    ren = [80] * 8
    prices = [50] * 4 + [10] * 4
    s = make_series(prices, ren)
    plan = best_window(
        s, now=T0, deadline=at(2), duration=H1, weights=Weights.from_priority(Priority.ECO)
    )
    assert plan.start == at(1)


# ── deadlines ─────────────────────────────────────────────────────────────────

RULES = [
    DeadlineRule(
        ready_by=time(17, 0), weekdays=WORKDAYS, plugged_from=time(6, 0), plugged_until=time(15, 0)
    ),
    DeadlineRule(ready_by=time(9, 30), plugged_from=time(15, 0), plugged_until=time(6, 0)),
]


def berlin(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


def test_workday_morning_ready_same_afternoon():
    d = resolve_deadline(berlin(2026, 11, 5, 8, 0), RULES, BERLIN)  # Thursday
    assert d == berlin(2026, 11, 5, 17, 0).astimezone(UTC)


def test_evening_ready_next_morning():
    d = resolve_deadline(berlin(2026, 11, 5, 19, 0), RULES, BERLIN)
    assert d == berlin(2026, 11, 6, 9, 30)


def test_after_midnight_ready_same_morning():
    d = resolve_deadline(berlin(2026, 11, 6, 1, 0), RULES, BERLIN)
    assert d == berlin(2026, 11, 6, 9, 30)


def test_weekend_morning_falls_back_to_default():
    plugged = berlin(2026, 11, 7, 10, 0)  # Saturday
    assert resolve_deadline(plugged, RULES, BERLIN) == plugged + timedelta(hours=12)


def test_override_wins():
    override = berlin(2026, 11, 5, 12, 0)
    assert resolve_deadline(berlin(2026, 11, 5, 8, 0), RULES, BERLIN, override=override) == override


def test_priority_beats_order():
    rules = [DeadlineRule(ready_by=time(17)), DeadlineRule(ready_by=time(12), priority=1)]
    assert resolve_deadline(berlin(2026, 11, 5, 8), rules, BERLIN) == berlin(2026, 11, 5, 12)


def test_deadline_across_dst_change():
    # 24→25 Oct 2026: 01:00 UTC offset changes from +2 to +1.
    d = resolve_deadline(berlin(2026, 10, 24, 20, 0), RULES, BERLIN)
    assert d == datetime(2026, 10, 25, 8, 30, tzinfo=UTC)  # 09:30 CET


def test_ready_by_in_spring_gap_is_normalised():
    rules = [DeadlineRule(ready_by=time(2, 30))]  # does not exist on 29 Mar 2026
    d = resolve_deadline(berlin(2026, 3, 28, 22, 0), rules, BERLIN)
    assert d.tzinfo == UTC
    assert d > berlin(2026, 3, 28, 22, 0)


# ── duration ──────────────────────────────────────────────────────────────────


def test_duration_without_history_is_the_users_estimate():
    assert estimate_duration([], timedelta(hours=2)) == timedelta(hours=2)


def test_duration_moves_smoothly_from_estimate_to_measurements():
    estimate = timedelta(hours=6)  # pessimistic guess → virtual charge of 6 h
    real = timedelta(hours=2)
    steps = [estimate_duration([real] * n, estimate) for n in range(0, 25)]
    assert steps[0] == timedelta(hours=6)
    # never jumps up, gets closer to the real 2 h with every charge
    assert all(b <= a for a, b in itertools.pairwise(steps))
    assert steps[1] > timedelta(hours=5)  # one real charge barely moves it
    assert steps[10] == real  # with ~10 charges the virtual one is outvoted
    assert steps[24] == real


def test_duration_uses_90th_percentile_of_real_charges():
    estimate = timedelta(hours=1)  # virtual 1 h, shorter than all real charges
    real = [timedelta(minutes=m) for m in (170, 175, 180, 180, 185, 190, 195, 200, 210, 240)]
    # 11 values (virtual + 10): p90 sits between the two longest real charges
    assert estimate_duration(real, estimate) == timedelta(minutes=210)


def test_duration_uses_only_recent_charges():
    past = [timedelta(hours=5)] * 30 + [timedelta(hours=2)] * 20
    assert estimate_duration(past, timedelta(hours=1)) == timedelta(hours=2)


def test_missing_green_share_does_not_hide_cheap_night_hours():
    # green share known only for the first 4 h (forecast ends at midnight), prices for 12 h:
    # the cheap night must still be considered, judged by price alone
    prices = [300.0] * 32 + [20.0] * 16
    ren = [50.0] * 16
    plan = best_window(
        make_series(prices, ren + [None] * 32),
        now=T0,
        deadline=at(12),
        duration=timedelta(hours=2),
        weights=Weights.from_priority(Priority.BALANCED),
    )
    assert plan.start == at(8)
    assert plan.chosen.avg_ren_share is None  # shown numbers stay real: none known there
    assert plan.chosen.avg_price == 20.0
    assert plan.green_ignored


def test_eco_first_plans_within_the_known_green_forecast():
    # evening very green, forecast ends at midnight (6 h), night cheaper but unknown green:
    # Eco first picks the green evening hours instead of guessing about the night
    prices = [100.0] * 16 + [95.0] * 8 + [60.0] * 24
    ren = [30.0] * 16 + [90.0] * 8
    plan = best_window(
        make_series(prices, ren + [None] * 24),
        now=T0,
        deadline=at(12),
        duration=timedelta(hours=2),
        weights=Weights.from_priority(Priority.ECO),
    )
    assert (plan.start, plan.end) == (at(4), at(6))
    assert plan.reason is PlanReason.PARTIAL_DATA  # replanned when the forecast grows
    assert not plan.green_ignored
    assert plan.chosen.avg_ren_share == 90.0


def test_eco_first_falls_back_to_price_when_the_forecast_is_too_short():
    # green known for 1 h only, the charge takes 2 h: the price alone decides
    prices = [300.0] * 32 + [20.0] * 16
    plan = best_window(
        make_series(prices, [50.0] * 4 + [None] * 44),
        now=T0,
        deadline=at(12),
        duration=timedelta(hours=2),
        weights=Weights.from_priority(Priority.ECO),
    )
    assert plan.start == at(8)
    assert plan.green_ignored
    assert plan.eco_choice is None


def test_ready_within_rules_and_the_fallback():
    rules = [
        DeadlineRule(within=timedelta(hours=6), plugged_from=time(8, 0), plugged_until=time(18, 0))
    ]
    plugged = berlin(2026, 11, 5, 10, 0)
    assert resolve_deadline(plugged, rules, BERLIN) == plugged + timedelta(hours=6)
    # no match: the fallback rule ("ready within" its hours)
    evening = berlin(2026, 11, 5, 20, 0)
    fallback = timedelta(hours=8)
    assert resolve_deadline(evening, rules, BERLIN, default=fallback) == evening + fallback


def test_estimate_counts_three_times_at_first():
    three_h = timedelta(hours=3)
    # one short real charge: [3 h, 3 h, 3 h, 1 h] → the 90 % value stays at 3 h
    assert estimate_duration([timedelta(hours=1)], three_h) == three_h
