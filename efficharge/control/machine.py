"""Per-device charging state machine: a pure function.

    step(state, event, ctx) -> (new_state, actions)

No I/O, no clock, no DB. The runner feeds events in, executes the returned
actions through the plug's device adapter and persists the new state.
``tests/test_machine.py`` walks through every transition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from efficharge.planner.window import Plan, PlanReason


class Phase(StrEnum):
    IDLE = "idle"  # plug ON, waiting for a battery
    PLANNED = "planned"  # plug OFF, waiting for the planned window
    CHARGING = "charging"  # plug ON, battery charging
    DONE = "done"  # plug ON, battery full (maybe still attached)
    PAUSED_BY_USER = "paused_by_user"  # user switched the plug off during charging
    OFF_BY_USER = "off_by_user"  # user switched the plug off while idle/done


class Override(StrEnum):
    BUTTON_ON = "button_on"
    BUTTON_OFF = "button_off"
    CHARGE_NOW = "charge_now"
    READY_BY = "ready_by"
    STOP = "stop"


class EndReason(StrEnum):
    FULL = "full"
    UNPLUGGED = "unplugged"
    TIMER = "timer"  # unmetered device: assumed full after the planned duration


# ── events ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PowerSample:
    watts: float


@dataclass(frozen=True)
class SwitchState:
    """Relay state as observed (push notification or poll). May repeat the known state."""

    on: bool


@dataclass(frozen=True)
class Tick:
    pass


@dataclass(frozen=True)
class Startup:
    """The runner (re)started. ``switch_on`` is the observed relay state, if readable."""

    switch_on: bool | None = None


@dataclass(frozen=True)
class Replan:
    """New prices were published or the settings changed."""


@dataclass(frozen=True)
class ManualPlugIn:
    """'I plugged in' button in the UI, for plugs without power metering."""


@dataclass(frozen=True)
class ChargeNow:
    pass


@dataclass(frozen=True)
class ReadyBy:
    deadline: datetime


@dataclass(frozen=True)
class StopCharging:
    pass


@dataclass(frozen=True)
class PlugOn:
    """UI: turn the plug back on after the user switched it off."""


Event = (
    PowerSample
    | SwitchState
    | Tick
    | Startup
    | Replan
    | ManualPlugIn
    | ChargeNow
    | ReadyBy
    | StopCharging
    | PlugOn
)


# ── actions ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnOn:
    pass


@dataclass(frozen=True)
class TurnOff:
    #: Ask the plug to switch itself back on at this time (dead-man's switch), if supported.
    auto_on_at: datetime | None = None


@dataclass(frozen=True)
class StartCycle:
    plugged_at: datetime


@dataclass(frozen=True)
class SavePlan:
    plan: Plan


@dataclass(frozen=True)
class RecordOverride:
    kind: Override
    at: datetime


@dataclass(frozen=True)
class Acknowledge:
    """Switch off briefly and on again: the rider sees the plug-in was noticed, even when
    charging continues right away. Ends with the plug ON."""


@dataclass(frozen=True)
class MarkCharging:
    """Charging actually started (for the cycle's ``actual_start``)."""

    at: datetime


@dataclass(frozen=True)
class FinishCycle:
    reason: EndReason
    at: datetime


Action = (
    TurnOn
    | TurnOff
    | Acknowledge
    | StartCycle
    | SavePlan
    | RecordOverride
    | MarkCharging
    | FinishCycle
)


# ── state and context ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    threshold_w: float = 2.0
    metered: bool = True
    plugin_samples: int = 2  # consecutive samples above threshold to detect a plug-in
    full_after: timedelta = timedelta(minutes=5)  # below threshold this long → full
    #: The charger switching off at the end: the power falls to standby out of the
    #: decline (the last reading below ``switch_off_share`` of the charge's peak, e.g.
    #: 20 W of 180 W). Then two low readings are enough. ``full_after`` (5 min) stays for
    #: drops from the plateau, where a short pause of the charger is possible.
    switch_off_share: float = 0.5
    switch_off_confirm: timedelta = timedelta(seconds=55)
    #: readings at most this old are still evidence after a restart (deploys take seconds)
    evidence_gap: timedelta = timedelta(minutes=2)
    unplugged_after: timedelta = timedelta(minutes=10)  # no draw after turn-on → unplugged
    rearm_after: timedelta = timedelta(minutes=10)  # DONE → IDLE after this long without draw
    start_slack: timedelta = timedelta(minutes=10)  # plan starts this soon → charge right away
    echo_window: timedelta = timedelta(seconds=15)  # switch reports within this are our own
    #: Battery taken off = power falls straight to (almost) nothing from at least this
    #: much AND from near the charge's peak (``sudden_drop_share``), and stays there for
    #: ``sudden_drop_confirm``. A full battery instead tapers off first (CV phase) and
    #: ends via ``full_after``; a short pause of the charger doesn't last long enough.
    sudden_drop_from_w: float = 20.0
    sudden_drop_share: float = 0.7
    sudden_drop_confirm: timedelta = timedelta(seconds=10)


#: (plugged_at, override_deadline, now) -> Plan. Built by the runner from DB data; must be
#: pure. ``now`` is the machine's own ``ctx.now``, so plan and decision use the same instant.
Planner = Callable[[datetime, datetime | None, datetime], Plan]


@dataclass(frozen=True)
class Context:
    now: datetime
    settings: Settings
    make_plan: Planner


@dataclass(frozen=True)
class DeviceState:
    phase: Phase = Phase.IDLE
    since: datetime | None = None
    switch_on: bool | None = None  # what we believe the relay is
    expected_switch: bool | None = None  # pending echo of our own command…
    expected_until: datetime | None = None  # …until this time
    plugged_at: datetime | None = None
    plan: Plan | None = None
    override_deadline: datetime | None = None
    above_count: int = 0
    low_since: datetime | None = None
    charging_since: datetime | None = None
    saw_load: bool = False
    last_watts: float | None = None  # previous power sample (sudden-drop detection)
    peak_watts: float = 0.0  # highest power of this charge
    drop_since: datetime | None = None  # a sudden drop started; confirmed if it lasts
    low_after_w: float | None = None  # the last reading before the power went low
    last_at: datetime | None = None  # time of the last power reading
    #: free-form, e.g. for logging; never used for decisions
    note: str = field(default="", compare=False)


Result = tuple[DeviceState, list[Action]]


# ── helpers ───────────────────────────────────────────────────────────────────


def _cmd(state: DeviceState, ctx: Context, on: bool) -> DeviceState:
    """Remember a switch command so its echo isn't mistaken for a button press."""
    return replace(
        state,
        switch_on=on,
        expected_switch=on,
        expected_until=ctx.now + ctx.settings.echo_window,
    )


def _enter(state: DeviceState, ctx: Context, phase: Phase, **changes) -> DeviceState:
    return replace(
        state,
        **{
            "phase": phase,
            "since": ctx.now,
            "above_count": 0,
            "low_since": None,
            "last_watts": None,
            "peak_watts": 0.0,
            "drop_since": None,
            **changes,
        },
    )


def _start_charging(
    state: DeviceState, ctx: Context, actions: list[Action], *, turn_on: bool
) -> DeviceState:
    if turn_on:
        actions.append(TurnOn())
        state = _cmd(state, ctx, True)
    else:
        state = replace(state, switch_on=True)
    actions.append(MarkCharging(ctx.now))
    return _enter(state, ctx, Phase.CHARGING, charging_since=ctx.now, saw_load=False)


def _apply_plan(
    state: DeviceState, ctx: Context, actions: list[Action], *, acknowledge: bool = False
) -> DeviceState:
    """(Re)plan and move to CHARGING or PLANNED accordingly.

    ``acknowledge``: a detected plug-in. If charging continues right away, blink the plug
    (off, on) so the rider still sees that the battery was noticed."""
    assert state.plugged_at is not None
    plan = ctx.make_plan(state.plugged_at, state.override_deadline, ctx.now)
    if ctx.now < plan.start <= ctx.now + ctx.settings.start_slack:
        # Record what really happens, so the UI can explain it: "the best window starts
        # at 10:45, that's in 9 minutes, so charging started right away".
        plan = replace(
            plan,
            start=ctx.now,
            end=ctx.now + plan.duration,
            reason=PlanReason.WINDOW_STARTS_SOON,
        )
    actions.append(SavePlan(plan))
    state = replace(state, plan=plan)
    if plan.start <= ctx.now + ctx.settings.start_slack:
        if acknowledge and state.switch_on is True:
            actions.append(Acknowledge())
            # both echoes (off, then on) arrive within the echo window and are ours
            state = _cmd(state, ctx, True)
            return _start_charging(state, ctx, actions, turn_on=False)
        return _start_charging(state, ctx, actions, turn_on=state.switch_on is not True)
    actions.append(TurnOff(auto_on_at=plan.start))
    state = _cmd(state, ctx, False)
    if state.phase == Phase.PLANNED:
        return state  # stay; keep `since`
    return _enter(state, ctx, Phase.PLANNED)


def _plug_in(
    state: DeviceState, ctx: Context, actions: list[Action], *, detected: bool = False
) -> DeviceState:
    actions.append(StartCycle(ctx.now))
    state = replace(state, plugged_at=ctx.now, override_deadline=None, plan=None)
    return _apply_plan(state, ctx, actions, acknowledge=detected)


def _finish(
    state: DeviceState,
    ctx: Context,
    actions: list[Action],
    reason: EndReason,
    phase: Phase,
    *,
    at: datetime | None = None,
) -> DeviceState:
    """``at``: when it really ended (e.g. when the power dropped), not when we were sure."""
    actions.append(FinishCycle(reason, at or ctx.now))
    return _enter(
        state,
        ctx,
        phase,
        plan=None,
        plugged_at=None,
        override_deadline=None,
        charging_since=None,
        saw_load=False,
    )


def _external_switch(state: DeviceState, ctx: Context, on: bool) -> bool:
    """True if a reported relay state is a *user* action (not our echo, not a repeat)."""
    if (
        state.expected_switch is not None
        and state.expected_until is not None
        and ctx.now <= state.expected_until
    ):
        # A command of ours is in flight. Only a report of the *opposite* state could
        # be the user, but it's more likely a stale report. Ignore it until the window ends.
        return False
    return state.switch_on is not None and on != state.switch_on


# ── the transition function ───────────────────────────────────────────────────


def needs_planner(state: DeviceState, event: Event, settings: Settings) -> bool:
    """Could ``step`` call ``make_plan`` for this event? (A cheap superset.)

    Building a planner costs several DB queries, so the runner only does it when this
    says so. Tests assert it for every step: no planning without a ``True`` here.
    """
    match event:
        case PowerSample(watts=w):
            return state.phase is Phase.IDLE and w > settings.threshold_w
        case ManualPlugIn():
            return state.phase in (Phase.IDLE, Phase.DONE)
        case Replan() | ReadyBy():
            return state.phase is Phase.PLANNED
    return False


def step(state: DeviceState, event: Event, ctx: Context) -> Result:
    actions: list[Action] = []
    s = ctx.settings

    # Clear an expired echo expectation.
    if state.expected_until is not None and ctx.now > state.expected_until:
        state = replace(state, expected_switch=None, expected_until=None)

    match event:
        case Startup(switch_on=observed):
            return _startup(state, ctx, observed)

        case SwitchState(on=on):
            if state.expected_switch is not None and on == state.expected_switch:
                # Our command's echo arrived: confirmed.
                return replace(
                    state, switch_on=on, expected_switch=None, expected_until=None
                ), actions
            if not _external_switch(state, ctx, on):
                if state.switch_on is None:
                    state = replace(state, switch_on=on)
                return state, actions
            state = replace(state, switch_on=on)
            return _user_switched(state, ctx, on, actions)

        case PowerSample(watts=w):
            return _power(state, ctx, w, actions)

        case Tick():
            if state.phase == Phase.PLANNED and state.plan and ctx.now >= state.plan.start:
                return _start_charging(state, ctx, actions, turn_on=True), actions
            if (
                state.phase == Phase.CHARGING
                and not s.metered
                and state.plan is not None
                and state.charging_since is not None
                and ctx.now >= state.charging_since + state.plan.duration
            ):
                return _finish(state, ctx, actions, EndReason.TIMER, Phase.DONE), actions
            return state, actions

        case Replan():
            if state.phase == Phase.PLANNED:
                return _apply_plan(state, ctx, actions), actions
            return state, actions

        case ManualPlugIn():
            if state.phase in (Phase.IDLE, Phase.DONE):
                return _plug_in(state, ctx, actions), actions
            return state, actions

        case ChargeNow():
            if state.phase in (Phase.PLANNED, Phase.PAUSED_BY_USER):
                actions.append(RecordOverride(Override.CHARGE_NOW, ctx.now))
                return _start_charging(state, ctx, actions, turn_on=True), actions
            return state, actions

        case ReadyBy(deadline=deadline):
            if state.phase == Phase.PLANNED:
                actions.append(RecordOverride(Override.READY_BY, ctx.now))
                state = replace(state, override_deadline=deadline)
                return _apply_plan(state, ctx, actions), actions
            return state, actions

        case StopCharging():
            if state.phase == Phase.CHARGING:
                actions += [RecordOverride(Override.STOP, ctx.now), TurnOff()]
                state = _cmd(state, ctx, False)
                return _enter(state, ctx, Phase.PAUSED_BY_USER), actions
            return state, actions

        case PlugOn():
            if state.phase == Phase.OFF_BY_USER:
                actions.append(TurnOn())
                return _enter(_cmd(state, ctx, True), ctx, Phase.IDLE), actions
            return state, actions

    raise TypeError(f"unknown event {event!r}")


#: a plug's own timer may fire a little before our clock says the window begins
TIMER_EARLY = timedelta(minutes=1)


def _user_switched(state: DeviceState, ctx: Context, on: bool, actions: list[Action]) -> Result:
    """The plug's button (or the vendor app) changed the relay. Never fight the user."""
    phase = state.phase
    if on:
        if phase == Phase.PLANNED:
            if state.plan is not None and ctx.now >= state.plan.start - TIMER_EARLY:
                # The window has begun: this is the plug's own timer (set with the
                # TurnOff as a safety net, see Capability.DEVICE_TIMER), not the user.
                # (A press at that very moment means the same anyway.)
                return _start_charging(state, ctx, actions, turn_on=False), actions
            actions.append(RecordOverride(Override.BUTTON_ON, ctx.now))
            return _start_charging(state, ctx, actions, turn_on=False), actions
        if phase == Phase.PAUSED_BY_USER:
            return _start_charging(state, ctx, actions, turn_on=False), actions
        if phase == Phase.OFF_BY_USER:
            return _enter(state, ctx, Phase.IDLE), actions
        return state, actions
    # switched off
    if phase == Phase.CHARGING:
        actions.append(RecordOverride(Override.BUTTON_OFF, ctx.now))
        return _enter(state, ctx, Phase.PAUSED_BY_USER), actions
    if phase in (Phase.IDLE, Phase.DONE):
        return _enter(state, ctx, Phase.OFF_BY_USER), actions
    return state, actions


def _power(state: DeviceState, ctx: Context, w: float, actions: list[Action]) -> Result:
    s = ctx.settings
    above = w > s.threshold_w

    match state.phase:
        case Phase.IDLE:
            count = state.above_count + 1 if above else 0
            state = replace(state, above_count=count, switch_on=True if above else state.switch_on)
            if count >= s.plugin_samples:
                return _plug_in(state, ctx, actions, detected=True), actions
            return state, actions

        case Phase.PLANNED | Phase.PAUSED_BY_USER | Phase.OFF_BY_USER:
            # The plug should be off. Real load means somebody switched it on: this is
            # the fallback button detection for adapters without switch reports.
            in_echo = state.expected_until is not None and ctx.now <= state.expected_until
            if above and not in_echo:
                return _user_switched(replace(state, switch_on=True), ctx, True, actions)
            return state, actions

        case Phase.CHARGING:
            previous = state.last_watts
            state = replace(
                state, last_watts=w, peak_watts=max(state.peak_watts, w), last_at=ctx.now
            )
            if above:
                return replace(
                    state, saw_load=True, low_since=None, drop_since=None, low_after_w=None
                ), actions
            if state.low_since is None:
                state = replace(state, low_since=ctx.now, low_after_w=previous)
            low_since = state.low_since
            # Battery taken off: from charging power straight to ~0 (a full battery tapers
            # off first). Not right after our own switching (the meter may read 0 briefly).
            if state.drop_since is not None:
                if ctx.now - state.drop_since >= s.sudden_drop_confirm:
                    ended = _finish(
                        state, ctx, actions, EndReason.UNPLUGGED, Phase.IDLE, at=state.drop_since
                    )
                    return ended, actions
                return state, actions
            in_echo = state.expected_until is not None and ctx.now <= state.expected_until
            if (
                previous is not None
                and previous >= s.sudden_drop_from_w
                and previous >= s.sudden_drop_share * state.peak_watts
                and not in_echo
            ):
                return replace(state, drop_since=ctx.now), actions
            switched_off = (
                state.low_after_w is not None
                and state.low_after_w < s.switch_off_share * state.peak_watts
                and ctx.now - low_since >= s.switch_off_confirm
            )
            if state.saw_load and (switched_off or ctx.now - low_since >= s.full_after):
                # the charge ended when the power dropped, not 5 minutes later
                ended = _finish(state, ctx, actions, EndReason.FULL, Phase.DONE, at=low_since)
                return ended, actions
            started = state.charging_since or state.since or ctx.now
            if not state.saw_load and ctx.now - started >= s.unplugged_after:
                return _finish(state, ctx, actions, EndReason.UNPLUGGED, Phase.IDLE), actions
            return state, actions

        case Phase.DONE:
            if above:
                return replace(state, low_since=None), actions
            low_since = state.low_since or ctx.now
            if ctx.now - low_since >= s.rearm_after:
                return _enter(state, ctx, Phase.IDLE), actions
            return replace(state, low_since=low_since), actions

    return state, actions


def _startup(state: DeviceState, ctx: Context, observed: bool | None) -> Result:
    """Reconcile after a restart. Fail-safe: when in doubt, ON."""
    actions: list[Action] = []
    state = replace(
        state,
        switch_on=observed,
        expected_switch=None,
        expected_until=None,
        above_count=0,
    )
    fresh = state.last_at is not None and ctx.now - state.last_at <= ctx.settings.evidence_gap
    if not fresh:
        # readings before a longer downtime say nothing about now (a deploy takes seconds:
        # then "low since 12:37" stays valid and the end isn't recorded minutes late)
        state = replace(state, low_since=None, last_watts=None, drop_since=None, low_after_w=None)
    match state.phase:
        case Phase.PLANNED if state.plan is not None and state.plan.start > ctx.now:
            if observed is True:
                # Switched on while we were down: most likely the button. Respect it.
                actions.append(RecordOverride(Override.BUTTON_ON, ctx.now))
                return _start_charging(state, ctx, actions, turn_on=False), actions
            actions.append(TurnOff(auto_on_at=state.plan.start))
            return _cmd(state, ctx, False), actions
        case Phase.PLANNED:
            return _start_charging(state, ctx, actions, turn_on=observed is not True), actions
        case Phase.PAUSED_BY_USER | Phase.OFF_BY_USER:
            return state, actions  # the user chose OFF; keep it
        case _:
            if state.phase == Phase.CHARGING:
                # Restart the "no load → unplugged" timer: the downtime is not evidence.
                state = replace(state, charging_since=ctx.now)
            if observed is not True:
                actions.append(TurnOn())
                state = _cmd(state, ctx, True)
            return state, actions
