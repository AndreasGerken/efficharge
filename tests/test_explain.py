from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

from efficharge.control.machine import DeviceState, Phase
from efficharge.db.models import Cycle
from efficharge.web import i18n
from efficharge.web.explain import explain
from tests.helpers import T0, at

TZ = ZoneInfo("Europe/Berlin")


def finished(end, reason="full") -> Cycle:
    return Cycle(
        plugged_at=end - timedelta(hours=4),
        actual_start=end - timedelta(hours=3),
        end_at=end,
        end_reason=reason,
        kwh=0.4,
    )


def test_after_a_charge_it_is_ready_again_and_shows_the_last_charge():
    i18n.set_locale("en")
    for phase in (Phase.IDLE, Phase.DONE):
        view = explain(DeviceState(phase=phase), None, finished(T0), TZ, at(1))
        assert view.headline == "Ready to charge"
        assert view.step == 0
        assert view.recent is not None and view.recent.kwh == 0.4


def test_the_last_charge_stays_visible_for_half_a_day():
    idle = DeviceState(phase=Phase.IDLE)
    assert explain(idle, None, finished(T0), TZ, at(11, 59)).recent is not None
    assert explain(idle, None, finished(T0), TZ, at(12)).recent is None


def test_ready_view_previews_the_next_window_and_savings():
    from efficharge.planner.window import Plan, PlanReason, WindowStats

    chosen = WindowStats(at(10), at(14), 100.0, 60.0)
    baseline = WindowStats(at(0), at(4), 300.0, 40.0)
    plan = Plan(at(10), at(14), at(20), timedelta(hours=4), PlanReason.OPTIMAL, chosen, baseline)
    view = explain(
        DeviceState(phase=Phase.IDLE), None, None, TZ, at(0), preview=plan, preview_kwh=0.5
    )
    assert view.sentence == "Charging would start at 23:00."
    assert [c.text for c in view.chips] == [
        "4:00 h estimated charge time",
        "Estimated cost 5.0 ct (-67%)",
        "Estimated +50% green power",
    ]
    unknown_energy = explain(DeviceState(phase=Phase.IDLE), None, None, TZ, at(0), preview=plan)
    assert [c.text for c in unknown_energy.chips][1] == "Estimated -67% cost"
    ignored = explain(
        DeviceState(phase=Phase.IDLE),
        None,
        None,
        TZ,
        at(0),
        preview=replace(plan, green_ignored=True),
    )
    assert [c.text for c in ignored.chips][1:] == [
        "Estimated -67% cost",
        "Green forecast not possible yet",
    ]


def test_a_waiting_charge_shows_estimates_like_a_finished_one_shows_results():
    from efficharge.control.serialize import plan_to_dict
    from efficharge.planner.window import Plan, PlanReason, WindowStats
    from efficharge.web.explain import cycle_chips

    i18n.set_locale("en")
    chosen = WindowStats(at(10), at(13), 100.0, 60.0)
    baseline = WindowStats(at(0), at(3), 150.0, 45.0)
    plan = Plan(
        at(10),
        at(13),
        at(20),
        timedelta(hours=3),
        PlanReason.OPTIMAL,
        chosen,
        baseline,
        eco_choice=chosen,
        money_choice=chosen,
    )
    waiting = Cycle(plugged_at=at(0), plan=plan_to_dict(plan))
    assert [c.text for c in cycle_chips(waiting, TZ, at(0), state=False, expected_kwh=0.33)] == [
        "Waiting until 23:00",
        "Eco and cost agree",
        "3:00 h estimated charge time",
        "Estimated cost 3.3 ct (-33%)",
        "Estimated +33% green power",
    ]
    done = finished(at(13))
    done.plan = plan_to_dict(plan)
    done.cost_actual_eur, done.cost_baseline_eur = 0.04, 0.06
    done.ren_actual_pct, done.ren_baseline_pct = 60.0, 45.0
    texts = [c.text for c in cycle_chips(done, TZ, at(14))]
    assert "Cost 4.0 ct (-33%)" in texts and "+33% green power" in texts


def test_first_reading_above_threshold_says_charger_detected():
    i18n.set_locale("en")
    view = explain(
        DeviceState(phase=Phase.IDLE, above_count=1), None, None, TZ, at(0), power_w=181.0
    )
    assert view.headline == "Charger detected"
    assert "181 W" in view.sentence
    assert view.step == 0


def test_paused_is_not_shown_as_charging():
    view = explain(DeviceState(phase=Phase.PAUSED_BY_USER), None, None, TZ, at(0))
    assert view.step_label == "Paused"
    assert "Charge now" in view.actions or "charge_now" in view.actions


def test_charger_on_standby_is_simply_ready():
    view = explain(DeviceState(phase=Phase.IDLE), None, None, TZ, at(0), power_w=0.8)
    assert view.headline == "Ready to charge"
    assert view.hint is None


def test_after_taking_the_battery_off():
    cycle = finished(T0, "unplugged")
    view = explain(DeviceState(phase=Phase.IDLE), None, cycle, TZ, at(0, 30), power_w=0.8)
    assert view.headline == "Ready to charge"
    assert view.recent is cycle
    later = explain(DeviceState(phase=Phase.IDLE), None, cycle, TZ, at(12), power_w=0.8)
    assert later.recent is None


def stored_plan(reason="optimal", agree=None) -> dict:
    """A plan as a cycle stores it (prices and green share the same as right away)."""
    from efficharge.control.serialize import plan_to_dict
    from efficharge.planner.window import Plan, PlanReason, WindowStats

    window = WindowStats(T0, at(3), 100.0, 50.0)
    other = window if agree else replace(window, start=at(5), end=at(8))
    choices = {} if agree is None else {"eco_choice": window, "money_choice": other}
    plan = Plan(
        T0, at(3), at(10), timedelta(hours=3), PlanReason(reason), window, window, **choices
    )
    return plan_to_dict(plan)


def chip_texts(cycle, now=None):
    from efficharge.web.explain import cycle_chips

    return [c.text for c in cycle_chips(cycle, TZ, now or at(6))]


def test_charge_labels_say_why_it_started_and_what_it_saved():
    cycle = finished(at(4))  # plugged 12:00 UTC, charged 13:00–16:00 UTC → waited 1 h
    cycle.plan = stored_plan()
    cycle.cost_actual_eur, cycle.cost_baseline_eur = 0.04, 0.10
    cycle.co2_actual_g, cycle.co2_baseline_g = 120.0, 220.0
    assert chip_texts(cycle) == [
        "Waited 1:00 h",
        "3:00 h charged",
        "400 Wh",
        "Cost 4.0 ct (-60%)",
        "6.0 ct saved, 100 g CO₂ avoided",
    ]


def test_savings_label_only_names_what_was_saved():
    cycle = finished(at(4))
    cycle.cost_actual_eur, cycle.cost_baseline_eur = 0.04, 0.10
    cycle.co2_actual_g, cycle.co2_baseline_g = 220.0, 200.0  # a bit more CO₂
    assert chip_texts(cycle)[-1] == "6.0 ct saved"


def test_charge_labels_for_starting_right_away():
    cycle = finished(at(4))
    cycle.actual_start = cycle.plugged_at
    for reason, text in [
        ("deadline_too_close", "Started right away: rule"),
        ("now_is_best", "Started right away: best window"),
        ("window_starts_soon", "Started right away: best window"),
        ("no_data", "Started right away: no forecast"),
    ]:
        cycle.plan = stored_plan(reason)
        assert chip_texts(cycle)[0] == text


def test_running_charge_shows_live_energy():
    from efficharge.web.data import Live
    from efficharge.web.explain import cycle_chips

    cycle = Cycle(
        plugged_at=T0,
        actual_start=T0,
        plan=stored_plan("now_is_best", agree=True),
    )
    texts = [c.text for c in cycle_chips(cycle, TZ, at(1), Live(0.096, 0.031))]
    assert texts == [
        "Started right away: best window",
        "Eco and cost agree",
        "1:00 h charged",
        "charging",
        "96 Wh",
        "Cost 3.1 ct",
    ]
    # on the status card the headline already says "charging"
    status = [c.text for c in cycle_chips(cycle, TZ, at(1), Live(0.096, 0.031), state=False)]
    assert "charging" not in status


def test_remaining_time_texts():
    from efficharge.control.remaining import Remaining
    from efficharge.web.explain import remaining_text

    m = timedelta(minutes=1)
    # 12:00 UTC = 13:00 Berlin; 90 % values, rounded to 5 min
    assert remaining_text(Remaining("plateau", 152 * m), T0, TZ) == (
        "⏱ Up to 2 h 30 min left (done around 15:30)."
    )
    assert remaining_text(Remaining("decline", 23 * m), T0, TZ) == (
        "⏱ About 25 min left (done around 13:25)."
    )
    assert remaining_text(Remaining("decline", timedelta(0)), T0, TZ) == "⏱ Done any moment now."
    assert remaining_text(None, T0, TZ) is None


def test_duration_label_formats():
    from efficharge.web.explain import short_duration

    assert short_duration(timedelta(minutes=45)) == "45 min"
    assert short_duration(timedelta(minutes=90)) == "1:30 h"


def test_the_priority_switch_shows_only_when_the_priority_chose():
    from efficharge.planner.window import Plan, PlanReason, WindowStats
    from efficharge.web.explain import priority_decides

    chosen = WindowStats(at(10), at(14), 100.0, 60.0)
    baseline = WindowStats(at(0), at(4), 300.0, 40.0)
    plan = Plan(at(10), at(14), at(20), timedelta(hours=4), PlanReason.OPTIMAL, chosen, baseline)
    assert priority_decides(plan)
    # the green forecast didn't reach far enough: the price alone decided, still chosen
    assert priority_decides(replace(plan, green_ignored=True))
    # started right away because of the rule: no priority involved
    assert not priority_decides(replace(plan, reason=PlanReason.DEADLINE_TOO_CLOSE))
    assert not priority_decides(None)


def test_charging_now_names_what_now_is_best_at():
    """Only "cheapest and greenest" when eco and cost agree; otherwise what was chosen."""
    from efficharge.planner.window import Plan, PlanReason, WindowStats
    from efficharge.web.explain import _reason_sentence

    i18n.set_locale("en")
    now = WindowStats(T0, at(3), 120.0, 40.0)  # starts now
    later = WindowStats(at(5), at(8), 80.0, 30.0)

    def sentence(chosen, eco, money, green_ignored=False):
        plan = Plan(
            T0,
            at(3),
            at(10),
            timedelta(hours=3),
            PlanReason.NOW_IS_BEST,
            chosen,
            chosen,
            eco_choice=eco,
            money_choice=money,
            green_ignored=green_ignored,
        )
        return _reason_sentence(plan, lambda dt: "22:00")

    assert "cheapest and greenest time" in sentence(now, now, now)
    assert "right now is the greenest time" in sentence(now, now, later)  # eco first
    assert "right now is the cheapest time" in sentence(now, later, now)  # cost first
    mix = WindowStats(T0 + timedelta(minutes=15), at(3), 100.0, 35.0)
    assert "best mix of green power and price" in sentence(mix, now, later)
    assert "right now is the cheapest time" in sentence(now, now, later, green_ignored=True)


def test_waiting_headline_names_what_the_window_is_best_at():
    from efficharge.planner.window import Plan, PlanReason, WindowStats
    from efficharge.web.explain import _waiting_headline

    i18n.set_locale("en")
    a = WindowStats(at(5), at(8), 80.0, 60.0)
    b = WindowStats(at(1), at(4), 60.0, 40.0)

    def headline(chosen, eco, money):
        plan = Plan(
            chosen.start,
            chosen.end,
            at(10),
            timedelta(hours=3),
            PlanReason.OPTIMAL,
            chosen,
            b,
            eco_choice=eco,
            money_choice=money,
        )
        return _waiting_headline(plan)

    assert headline(a, a, a) == "Waiting for cheap, green power"
    assert headline(a, a, b) == "Waiting for green power"
    assert headline(b, a, b) == "Waiting for cheap power"
    mid = WindowStats(at(3), at(6), 70.0, 50.0)
    assert headline(mid, a, b) == "Waiting for the best mix of green power and price"
