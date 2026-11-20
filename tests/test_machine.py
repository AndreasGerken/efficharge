from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from efficharge.control.machine import (
    Acknowledge,
    ChargeNow,
    Context,
    DeviceState,
    EndReason,
    FinishCycle,
    ManualPlugIn,
    MarkCharging,
    Override,
    Phase,
    PlugOn,
    PowerSample,
    ReadyBy,
    RecordOverride,
    Replan,
    SavePlan,
    Settings,
    StartCycle,
    Startup,
    StopCharging,
    SwitchState,
    Tick,
    TurnOff,
    TurnOn,
    needs_planner,
    step,
)
from efficharge.planner.window import Plan, PlanReason, WindowStats
from tests.helpers import T0, at

DURATION = timedelta(hours=3)


@dataclass
class FakePlanner:
    """Plans a window starting ``start_in`` after now; honours an override deadline."""

    start_in: timedelta = timedelta(hours=2)
    now: datetime = T0
    calls: list = field(default_factory=list)

    def __call__(self, plugged_at: datetime, override: datetime | None, now: datetime) -> Plan:
        self.calls.append((plugged_at, override))
        self.now = now
        start = self.now + self.start_in
        deadline = override or plugged_at + timedelta(hours=12)
        if start + DURATION > deadline:
            start = self.now
        w = WindowStats(start, start + DURATION, 10.0, 80.0)
        base = WindowStats(self.now, self.now + DURATION, 50.0, 40.0)
        return Plan(start, start + DURATION, deadline, DURATION, PlanReason.OPTIMAL, w, base)


class Sim:
    """Drives the pure machine like the runner would, with a manual clock."""

    def __init__(self, settings: Settings | None = None, start_in: timedelta | None = None):
        self.settings = settings or Settings()
        self.planner = FakePlanner(**({"start_in": start_in} if start_in is not None else {}))
        self.state = DeviceState(phase=Phase.IDLE, since=T0, switch_on=True)
        self.now = T0
        self.log: list = []

    def at(self, t: datetime) -> Sim:
        self.now = t
        self.planner.now = t
        return self

    def send(self, event) -> list:
        ctx = Context(now=self.now, settings=self.settings, make_plan=self.planner)
        calls_before = len(self.planner.calls)
        expected = needs_planner(self.state, event, self.settings)
        self.state, actions = step(self.state, event, ctx)
        if len(self.planner.calls) > calls_before:
            assert expected, f"planned on {event!r} in {self.state.phase} without needs_planner"
        self.log += actions
        return actions

    def plug_in(self) -> list:
        self.send(PowerSample(120))
        return self.send(PowerSample(120))

    @property
    def phase(self) -> Phase:
        return self.state.phase


def kinds(actions) -> list[type]:
    return [type(a) for a in actions]


def planned_sim(**kw) -> Sim:
    sim = Sim(**kw)
    sim.plug_in()
    assert sim.phase is Phase.PLANNED
    sim.at(T0 + timedelta(seconds=20)).send(SwitchState(False))  # echo of our TurnOff
    return sim


# ── normal cycle ──────────────────────────────────────────────────────────────


def test_full_cycle_plan_wait_charge_done_rearm():
    sim = Sim()
    assert sim.send(PowerSample(120)) == []  # debounce: one sample is not enough
    actions = sim.send(PowerSample(120))
    assert kinds(actions) == [StartCycle, SavePlan, TurnOff]
    assert actions[2] == TurnOff(auto_on_at=at(2))
    assert sim.phase is Phase.PLANNED

    assert sim.at(at(1)).send(Tick()) == []
    actions = sim.at(at(2)).send(Tick())
    assert kinds(actions) == [TurnOn, MarkCharging]
    assert sim.phase is Phase.CHARGING

    sim.at(at(2, 1)).send(PowerSample(140))
    sim.at(at(4, 50)).send(PowerSample(30))  # CV phase: the charger tapers off
    sim.at(at(5)).send(PowerSample(0.5))
    assert sim.phase is Phase.CHARGING  # not low long enough yet
    actions = sim.at(at(5, 5)).send(PowerSample(0.5))
    # the charger switched off out of the decline: confirmed with the next readings,
    # recorded when the power dropped
    assert actions == [FinishCycle(EndReason.FULL, at(5))]
    assert sim.phase is Phase.DONE

    sim.at(at(5, 10)).send(PowerSample(0.4))
    assert sim.phase is Phase.DONE
    sim.at(at(5, 21)).send(PowerSample(0.4))
    assert sim.phase is Phase.IDLE


def test_best_window_is_now_blinks_to_acknowledge_then_charges():
    sim = Sim(start_in=timedelta(0))
    actions = sim.plug_in()
    assert kinds(actions) == [StartCycle, SavePlan, Acknowledge, MarkCharging]
    assert sim.phase is Phase.CHARGING
    # the plug's own reports of the blink (off, then on) are not a button press
    assert sim.at(T0 + timedelta(seconds=1)).send(SwitchState(False)) == []
    assert sim.at(T0 + timedelta(seconds=3)).send(SwitchState(True)) == []
    assert sim.phase is Phase.CHARGING
    # a real press later still counts
    sim.at(at(0, 30)).send(SwitchState(False))
    assert sim.phase is Phase.PAUSED_BY_USER


def test_manual_plug_in_does_not_blink():
    sim = Sim(settings=Settings(metered=False), start_in=timedelta(0))
    actions = sim.send(ManualPlugIn())
    assert Acknowledge not in kinds(actions)


def test_start_within_slack_charges_right_away_and_says_why():
    sim = Sim(start_in=timedelta(minutes=5))
    actions = sim.plug_in()
    assert sim.phase is Phase.CHARGING
    plan = next(a.plan for a in actions if isinstance(a, SavePlan))
    assert plan.reason is PlanReason.WINDOW_STARTS_SOON
    assert plan.start == T0  # what really happens…
    assert plan.chosen.start == at(0, 5)  # …and the window it was going to wait for
    assert plan.end - plan.start == DURATION


def test_spike_below_debounce_is_ignored():
    sim = Sim()
    sim.send(PowerSample(120))
    sim.send(PowerSample(0.3))
    sim.send(PowerSample(120))
    assert sim.phase is Phase.IDLE


def test_charger_dip_does_not_end_cycle():
    sim = Sim(start_in=timedelta(0))
    sim.plug_in()
    sim.at(at(0, 10)).send(PowerSample(120))
    sim.at(at(1)).send(PowerSample(1))
    sim.at(at(1, 3)).send(PowerSample(80))  # CV phase dip ended
    sim.at(at(1, 7)).send(PowerSample(1))
    assert sim.phase is Phase.CHARGING
    sim.at(at(1, 12)).send(PowerSample(1))
    assert sim.phase is Phase.DONE


# ── the plug button (section 4.1) ─────────────────────────────────────────────


def test_button_on_while_planned_charges_now_and_records_override():
    sim = planned_sim()
    actions = sim.at(at(0, 30)).send(SwitchState(True))
    assert actions == [RecordOverride(Override.BUTTON_ON, at(0, 30)), MarkCharging(at(0, 30))]
    assert TurnOn not in kinds(actions)  # it is already on
    assert sim.phase is Phase.CHARGING


def test_plug_timer_switching_on_at_the_window_start_is_not_a_button_press():
    sim = planned_sim()
    start = sim.state.plan.start
    actions = sim.at(start + timedelta(seconds=11)).send(SwitchState(True))
    assert actions == [MarkCharging(start + timedelta(seconds=11))]  # no RecordOverride
    assert sim.phase is Phase.CHARGING


def test_echo_of_own_command_is_not_a_button_press():
    sim = Sim()
    sim.plug_in()  # we turn it off
    # a stale "on" report racing with our command is ignored inside the echo window
    assert sim.at(T0 + timedelta(seconds=5)).send(SwitchState(True)) == []
    assert sim.phase is Phase.PLANNED
    assert sim.at(T0 + timedelta(seconds=10)).send(SwitchState(False)) == []
    # repeated polls of the same state are not presses either
    assert sim.at(at(0, 30)).send(SwitchState(False)) == []
    assert sim.phase is Phase.PLANNED


def test_fallback_detection_by_power_while_planned():
    sim = planned_sim()
    actions = sim.at(at(0, 40)).send(PowerSample(130))
    assert RecordOverride(Override.BUTTON_ON, at(0, 40)) in actions
    assert sim.phase is Phase.CHARGING


def test_power_reading_inside_echo_window_is_ignored():
    sim = Sim()
    sim.plug_in()
    sim.at(T0 + timedelta(seconds=3)).send(PowerSample(130))  # reading from before the switch
    assert sim.phase is Phase.PLANNED


def test_button_off_while_charging_pauses_and_we_do_not_fight_it():
    sim = Sim(start_in=timedelta(0))
    sim.plug_in()
    sim.at(at(0, 20)).send(SwitchState(False))
    assert sim.phase is Phase.PAUSED_BY_USER
    assert sim.log[-1] == RecordOverride(Override.BUTTON_OFF, at(0, 20))
    assert sim.at(at(1)).send(PowerSample(0)) == []
    assert sim.at(at(1)).send(Tick()) == []
    sim.at(at(2)).send(SwitchState(True))
    assert sim.phase is Phase.CHARGING


def test_button_off_while_idle_and_plug_on_from_ui():
    sim = Sim()
    sim.send(SwitchState(False))
    assert sim.phase is Phase.OFF_BY_USER
    assert sim.send(PowerSample(0)) == []
    assert sim.send(PlugOn()) == [TurnOn()]
    assert sim.phase is Phase.IDLE


# ── UI commands, replanning ──────────────────────────────────────────────────


def test_charge_now_from_ui():
    sim = planned_sim()
    actions = sim.at(at(0, 30)).send(ChargeNow())
    assert kinds(actions) == [RecordOverride, TurnOn, MarkCharging]
    assert sim.phase is Phase.CHARGING


def test_stop_charging_from_ui():
    sim = Sim(start_in=timedelta(0))
    sim.plug_in()
    actions = sim.send(StopCharging())
    assert kinds(actions) == [RecordOverride, TurnOff]
    assert sim.phase is Phase.PAUSED_BY_USER
    sim.send(ChargeNow())
    assert sim.phase is Phase.CHARGING


def test_replan_moves_window_and_updates_device_timer():
    sim = planned_sim()
    sim.planner.start_in = timedelta(hours=4)
    actions = sim.at(at(1)).send(Replan())
    assert kinds(actions) == [SavePlan, TurnOff]
    assert actions[1].auto_on_at == at(5)
    assert sim.phase is Phase.PLANNED
    assert sim.state.since == T0  # still the same waiting period
    assert sim.planner.calls[-1][0] == T0  # replanned from the original plug-in time


def test_ready_by_override_that_leaves_no_time_starts_now():
    sim = planned_sim()
    actions = sim.at(at(0, 30)).send(ReadyBy(at(3)))
    assert RecordOverride(Override.READY_BY, at(0, 30)) in actions
    assert sim.planner.calls[-1] == (T0, at(3))
    assert sim.phase is Phase.CHARGING


def test_replan_is_ignored_unless_planned():
    sim = Sim()
    assert sim.send(Replan()) == []


# ── failure handling ──────────────────────────────────────────────────────────


def test_battery_unplugged_while_planned_is_detected_at_start():
    sim = planned_sim()
    sim.at(at(2)).send(Tick())
    sim.at(at(2, 5)).send(PowerSample(0))
    assert sim.phase is Phase.CHARGING
    actions = sim.at(at(2, 10)).send(PowerSample(0))
    assert actions == [FinishCycle(EndReason.UNPLUGGED, at(2, 10))]
    assert sim.phase is Phase.IDLE


def test_startup_planned_in_future_makes_sure_plug_is_off():
    sim = planned_sim()
    actions = sim.at(at(1)).send(Startup(switch_on=None))
    assert actions == [TurnOff(auto_on_at=at(2))]
    assert sim.phase is Phase.PLANNED


def test_startup_after_missed_start_turns_on():
    sim = planned_sim()
    actions = sim.at(at(3)).send(Startup(switch_on=False))
    assert kinds(actions) == [TurnOn, MarkCharging]
    assert sim.phase is Phase.CHARGING


def test_startup_finds_plug_switched_on_while_down():
    sim = planned_sim()
    actions = sim.at(at(1)).send(Startup(switch_on=True))
    assert actions[0] == RecordOverride(Override.BUTTON_ON, at(1))
    assert sim.phase is Phase.CHARGING


def test_startup_when_in_doubt_on():
    sim = Sim()
    assert sim.send(Startup(switch_on=None)) == [TurnOn()]
    sim = Sim()
    assert sim.send(Startup(switch_on=True)) == []


def test_startup_respects_user_off():
    sim = Sim()
    sim.send(SwitchState(False))
    assert sim.send(Startup(switch_on=False)) == []
    assert sim.phase is Phase.OFF_BY_USER


def test_startup_while_charging_resets_unplugged_timer():
    sim = Sim(start_in=timedelta(0))
    sim.plug_in()
    sim.at(at(1)).send(Startup(switch_on=True))
    sim.at(at(1, 5)).send(PowerSample(0))
    assert sim.phase is Phase.CHARGING  # downtime does not count as "no load"


# ── devices without power metering ────────────────────────────────────────────


def test_unmetered_device_manual_plug_in_and_timer_end():
    sim = Sim(settings=Settings(metered=False))
    actions = sim.send(ManualPlugIn())
    assert kinds(actions) == [StartCycle, SavePlan, TurnOff]
    sim.at(at(2)).send(Tick())
    assert sim.phase is Phase.CHARGING
    assert sim.at(at(4, 59)).send(Tick()) == []
    actions = sim.at(at(5)).send(Tick())
    assert actions == [FinishCycle(EndReason.TIMER, at(5))]
    assert sim.phase is Phase.DONE


# ── battery taken off while charging ──────────────────────────────────────────


def charging_sim() -> Sim:
    sim = Sim(start_in=timedelta(0))
    sim.plug_in()
    sim.at(at(0, 10)).send(PowerSample(181))
    sim.at(at(0, 11)).send(PowerSample(181))
    return sim


def test_sudden_drop_is_recognised_as_unplugged_within_seconds():
    sim = charging_sim()
    t = at(0, 12)
    assert sim.at(t).send(PowerSample(0)) == []  # first sign, not yet confirmed
    assert sim.at(t + timedelta(seconds=5)).send(PowerSample(0)) == []
    actions = sim.at(t + timedelta(seconds=10)).send(PowerSample(0))
    assert actions == [FinishCycle(EndReason.UNPLUGGED, t)]  # when it was taken off
    assert sim.phase is Phase.IDLE


def test_short_pause_of_the_charger_is_not_an_unplug():
    sim = charging_sim()
    t = at(0, 12)
    sim.at(t).send(PowerSample(0))
    sim.at(t + timedelta(seconds=5)).send(PowerSample(0))
    sim.at(t + timedelta(seconds=8)).send(PowerSample(181))  # back after 8 s
    sim.at(t + timedelta(seconds=40)).send(PowerSample(181))
    assert sim.phase is Phase.CHARGING


def test_cut_off_after_tapering_counts_as_full_not_unplugged():
    sim = charging_sim()
    sim.at(at(2)).send(PowerSample(60))
    sim.at(at(2, 10)).send(PowerSample(25))  # tapered: 25 W < 70 % of the 181 W peak
    sim.at(at(2, 11)).send(PowerSample(0))
    sim.at(
        at(
            2,
            11,
        )
        + timedelta(seconds=30)
    ).send(PowerSample(0))
    assert sim.phase is Phase.CHARGING  # no sudden-drop finish
    sim.at(at(2, 16)).send(PowerSample(0))
    assert sim.log[-1] == FinishCycle(EndReason.FULL, at(2, 11))  # the charger's cut-off


def test_zero_reading_right_after_the_acknowledge_blink_is_ignored():
    sim = Sim(start_in=timedelta(0))
    sim.send(PowerSample(181))
    sim.send(PowerSample(181))  # detected → blink → charging
    sim.at(T0 + timedelta(seconds=3)).send(PowerSample(0))  # meter still catching up
    sim.at(T0 + timedelta(seconds=30)).send(PowerSample(0))
    assert sim.phase is Phase.CHARGING


def test_demo_speed_stretches_the_echo_window():
    """At 300× our own switch report arrives "minutes" later on the demo clock: it must
    still count as our echo, not as a button press."""
    from efficharge.control.machine import Settings
    from efficharge.control.runner import DeviceRunner, InMemoryStore

    class FastClock:
        speed = 300.0

        def __call__(self):
            return T0

    runner = DeviceRunner("1", None, None, InMemoryStore(), Settings(), None, FastClock())
    assert runner._settings_now().echo_window == Settings().echo_window * 300
    runner.clock = lambda: T0  # the real clock: unchanged
    assert runner._settings_now() is runner.settings


def test_charger_switching_off_after_the_decline_is_full_within_a_minute():
    sim = charging_sim()  # 181 W
    sim.at(at(2)).send(PowerSample(19.6))  # end of the decline
    t = at(2, 0) + timedelta(seconds=30)
    sim.at(t).send(PowerSample(1.0))  # switched off
    assert sim.phase is Phase.CHARGING
    sim.at(t + timedelta(seconds=30)).send(PowerSample(0.8))
    assert sim.phase is Phase.CHARGING  # one more reading
    actions = sim.at(t + timedelta(seconds=60)).send(PowerSample(0.8))
    assert actions == [FinishCycle(EndReason.FULL, t)]


def test_low_power_straight_from_the_plateau_still_waits_five_minutes():
    # a small charger (15 W): below the sudden-drop size, but no decline before it
    sim = Sim(start_in=timedelta(0))
    sim.plug_in()
    sim.at(at(0, 5)).send(PowerSample(15))
    sim.at(at(1)).send(PowerSample(15))
    sim.at(at(1, 1)).send(PowerSample(0.8))
    sim.at(at(1, 3)).send(PowerSample(0.8))
    assert sim.phase is Phase.CHARGING  # could be a pause of the charger
    actions = sim.at(at(1, 6)).send(PowerSample(0.8))
    assert actions == [FinishCycle(EndReason.FULL, at(1, 1))]


def test_a_quick_restart_keeps_the_evidence():
    sim = charging_sim()
    sim.at(at(2)).send(PowerSample(19.6))
    sim.at(at(2, 1)).send(PowerSample(1.0))
    sim.at(at(2, 2)).send(Startup(switch_on=True))  # a deploy, one minute later
    actions = sim.at(at(2, 2) + timedelta(seconds=30)).send(PowerSample(0.8))
    assert actions == [FinishCycle(EndReason.FULL, at(2, 1))]  # not reset to the restart
