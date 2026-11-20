"""Turn the machine's state + plan reason codes into one plain sentence.

The core never produces text. This module is the only place that does, and every
string goes through gettext.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from efficharge.control.machine import DeviceState, Phase
from efficharge.control.remaining import Remaining
from efficharge.control.serialize import plan_from_dict
from efficharge.db.models import Cycle
from efficharge.planner.window import Plan, PlanReason
from efficharge.web.i18n import _, fmt_ct, fmt_day_time, fmt_duration, fmt_eur, fmt_num, fmt_pct

#: the stepper: ready → waiting → charging, and back to ready (a full battery is
#: "ready for the next charge" from efficharge's point of view)
STEPS = ("ready", "waiting", "charging")


@dataclass
class Chip:
    text: str
    kind: str = "neutral"  # neutral | good | info


@dataclass
class StatusView:
    phase: str
    step: int  # index into STEPS, -1 = no cycle
    headline: str
    sentence: str
    chips: list[Chip] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # charge_now | stop | plug_on | ready_by
    hint: str | None = None
    #: replaces the label of the current step (e.g. "Paused" instead of "Charging")
    step_label: str | None = None
    #: the charge that ended in the last 12 hours, shown below the status
    recent: Cycle | None = None
    #: the priority chose the window: show the switch (see ``priority_decides``)
    show_priority: bool = False
    #: the price alone decides for now (the green forecast doesn't reach far enough)
    price_only: bool = False


def step_labels() -> list[str]:
    return [_("Ready"), _("Waiting"), _("Charging")]


def _change(value: float | None, baseline: float | None) -> float | None:
    """Relative change in % against charging right away; None if unknown or below 1 %."""
    if value is None or not baseline:
        return None
    change = (value - baseline) / abs(baseline) * 100
    return change if abs(change) >= 1 else None


def _cost_chip(eur: float | None, change: float | None, *, estimated: bool) -> Chip | None:
    """'Cost 3.3 ct (−33 %)'; estimated before the charge. Without an energy estimate
    only the change: 'Estimated −33 % cost'."""
    kind = "good" if change is not None and change < 0 else "neutral"
    pct = fmt_pct(change, signed=True) if change is not None else None
    if eur is None:
        return Chip(_("Estimated %(change)s cost", change=pct), kind) if pct else None
    money = fmt_money(eur)
    if estimated:
        text = (
            _("Estimated cost %(money)s (%(change)s)", money=money, change=pct)
            if pct
            else _("Estimated cost %(money)s", money=money)
        )
    else:
        text = (
            _("Cost %(money)s (%(change)s)", money=money, change=pct)
            if pct
            else _("Cost %(money)s", money=money)
        )
    return Chip(text, kind)


def _green_chip(change: float | None, *, estimated: bool) -> Chip | None:
    """'+33 % green power' compared with charging right away."""
    if change is None:
        return None
    pct = fmt_pct(change, signed=True)
    text = (
        _("Estimated %(change)s green power", change=pct)
        if estimated
        else _("%(change)s green power", change=pct)
    )
    return Chip(text, "good" if change > 0 else "neutral")


def _agree_chip(plan: Plan) -> Chip | None:
    if plan.eco_choice is None or plan.money_choice is None:
        return None
    if plan.priorities_agree:
        return Chip(_("Eco and cost agree"), "info")
    return Chip(_("Eco and cost disagree today"))


def _estimate_chips(plan: Plan, kwh: float | None) -> list[Chip]:
    """How a planned charge will probably go: cost and green power of the chosen window
    compared with charging right away. ``kwh``: the expected energy (None: unknown)."""
    c, b = plan.chosen, plan.baseline
    eur = c.avg_price * kwh / 1000 if kwh and c.avg_price is not None else None  # EUR/MWh
    chips = [_cost_chip(eur, _change(c.avg_price, b.avg_price), estimated=True)]
    if plan.green_ignored:
        chips.append(Chip(_("Green forecast not possible yet"), "info"))
    else:
        chips.append(_green_chip(_change(c.avg_ren_share, b.avg_ren_share), estimated=True))
    return [chip for chip in chips if chip is not None]


def explain(
    state: DeviceState,
    cycle: Cycle | None,
    last_cycle: Cycle | None,
    tz: ZoneInfo,
    now: datetime,
    metered: bool = True,
    power_w: float | None = None,
    remaining: Remaining | None = None,
    preview: Plan | None = None,
    preview_kwh: float | None = None,
) -> StatusView:
    """The status card. The chips of a planned or running charge come from
    ``cycle_chips`` (the same as in the list of charges)."""
    plan = state.plan

    def t(dt: datetime | None, ref: datetime | None = None) -> str:
        """Day only when it differs from ``ref`` (default: today)."""
        return fmt_day_time(dt, tz, ref or now)

    match state.phase:
        case Phase.IDLE if state.above_count > 0:
            # one reading above the threshold, the second one (seconds later) confirms it
            return StatusView(
                "detecting",
                0,
                _("Charger detected"),
                _(
                    "The plug measures %(w)s W. Efficharge checks once more and plans the charge "
                    "in a few seconds.",
                    w=fmt_num(power_w or 0, "0"),
                ),
            )

        case Phase.IDLE | Phase.DONE:
            return _ready_view(last_cycle, now, metered, preview, preview_kwh, t)

        case Phase.PLANNED if plan is not None:
            sentence = _(
                "Waiting for the best window %(start)s–%(end)s.",  # the rule line has the deadline
                start=t(plan.start),
                end=t(plan.end, plan.start),
            )
            if plan.reason is PlanReason.PARTIAL_DATA:
                sentence = _(
                    "Planned with the forecasts known so far: %(start)s–%(end)s. "
                    "New forecasts come during the day, the plan may get better then.",
                    start=t(plan.start),
                    end=t(plan.end),
                )
            return StatusView(
                "planned",
                1,
                _waiting_headline(plan),
                sentence,
                actions=["charge_now", "ready_by"],
                hint=_("Need it now? Press the plug button."),
            )

        case Phase.CHARGING:
            override = cycle.override if cycle else None
            if override in ("stop", "button_off"):
                sentence = _("Charging again after your pause.")
            elif override in ("button_on", "charge_now"):
                how = _("with the plug button") if override == "button_on" else _("in the app")
                sentence = _(
                    "You started charging %(how)s at %(at)s.", how=how, at=t(cycle.override_at)
                )
                if plan is not None and plan.reason not in _STARTS_NOW:
                    sentence += " " + _(
                        "The planned window was %(start)s–%(end)s.",
                        start=t(plan.start),
                        end=t(plan.end),
                    )
            elif plan is None:
                sentence = _("Charging.")
            else:
                sentence = _reason_sentence(plan, t)
            return StatusView(
                "charging",
                2,
                _("Charging"),
                sentence,
                actions=["stop"],
                hint=remaining_text(remaining, now, tz),
            )

        case Phase.PAUSED_BY_USER:
            return StatusView(
                "paused",
                2,
                _("Paused by you"),
                _(
                    "The plug is off because you switched it off. Press the plug button or "
                    "“Charge now” to continue."
                ),
                actions=["charge_now"],
                step_label=_("Paused"),
            )

        case Phase.OFF_BY_USER:
            return StatusView(
                "off",
                -1,
                _("Plug switched off"),
                _(
                    "You switched the plug off, so efficharge can't see when a battery is "
                    "plugged in. Switch it on again to continue."
                ),
                actions=["plug_on"],
            )

    return StatusView(state.phase.value, -1, _("Status"), "")


#: plan reasons where a window was really chosen by the priority (not "start now" by a rule)
_CHOSEN_BY_PRIORITY = {
    PlanReason.OPTIMAL,
    PlanReason.PARTIAL_DATA,
    PlanReason.WINDOW_STARTS_SOON,
    PlanReason.NOW_IS_BEST,
}


def priority_decides(plan: Plan | None) -> bool:
    """Whether the user's priority chose the window (not "start now" by a rule or for
    lack of data): then the status card shows the priority switch."""
    return plan is not None and plan.reason in _CHOSEN_BY_PRIORITY


#: a finished charge stays visible below the status this long (until the next plug-in)
SHOW_RECENT_FOR = timedelta(hours=12)


def _ready_view(
    last: Cycle | None,
    now: datetime,
    metered: bool,
    preview: Plan | None,
    preview_kwh: float | None,
    t,
) -> StatusView:
    """Nothing charging: the window a plug-in now would get, and the last charge if recent."""
    actions, hint = [], None
    if not metered:
        hint = _("This plug can't measure power. Plug in your charger, then press “I plugged in”.")
        actions = ["plugged_in"]
    sentence, chips = _("Efficharge plans the cheapest and greenest time to charge."), []
    if preview is not None:  # the same estimates a plugged-in charge shows
        sentence = _preview_sentence(preview, now, t)
        chips = [
            chip
            for chip in (_agree_chip(preview), _estimate_chip(preview.duration))
            if chip is not None
        ] + _estimate_chips(preview, preview_kwh)
    return StatusView(
        "idle",
        0,
        _("Ready to charge"),
        sentence,
        chips,
        actions,
        hint=hint,
        recent=recent_charge(last, now),
    )


def _preview_sentence(plan: Plan, now: datetime, t) -> str:
    """What a plug-in now would do: start right away, or at what time (the window and the
    deadline are in the chart and the rule line)."""
    wait = plan.start - now
    if plan.reason not in (PlanReason.OPTIMAL, PlanReason.PARTIAL_DATA) or wait < timedelta(
        minutes=1
    ):
        return _("Charging would start right away.")
    return _("Charging would start at %(t)s.", t=t(plan.start))


def recent_charge(cycle: Cycle | None, now: datetime) -> Cycle | None:
    """The last charge, if it is still shown (below the status and in the chart)."""
    if (
        cycle is None
        or cycle.actual_start is None
        or cycle.end_at is None
        or now - cycle.end_at >= SHOW_RECENT_FOR
        or cycle.end_at - cycle.actual_start <= timedelta(minutes=1)  # never really charged
    ):
        return None
    return cycle


def _round5(d: timedelta) -> timedelta:
    """Estimates are rough: whole 5 minutes (at least 5)."""
    minutes = max(5, round(d.total_seconds() / 300) * 5)
    return timedelta(minutes=minutes)


def remaining_text(remaining: Remaining | None, now: datetime, tz: ZoneInfo) -> str | None:
    """While charging. "ready by" is reserved for the deadline; estimates say "around".

    plateau: "Up to 2 h 30 min left (done around 14:50)." (90 % value, like the plan)
    decline: "About 30 min left (done around 12:33)." (extrapolated from the readings)"""
    if remaining is None:
        return None
    if remaining.left < timedelta(minutes=2):
        return "⏱ " + _("Done any moment now.")
    left = _round5(remaining.left)
    when = fmt_day_time(now + left, tz, now)
    if remaining.kind == "decline":
        return "⏱ " + _("About %(d)s left (done around %(t)s).", d=fmt_duration(left), t=when)
    return "⏱ " + _("Up to %(d)s left (done around %(t)s).", d=fmt_duration(left), t=when)


def fmt_money(eur: float) -> str:
    """Small amounts in cents ("4,2 ct"), larger ones in euros."""
    return fmt_ct(eur) if abs(eur) < 1 else fmt_eur(eur)


_STARTS_NOW = {
    PlanReason.NOW_IS_BEST,
    PlanReason.WINDOW_STARTS_SOON,
    PlanReason.DEADLINE_TOO_CLOSE,
    PlanReason.NO_DATA,
}


def _best_at(plan: Plan | None) -> str:
    """What the chosen window is best at: 'both' (eco and cost agree, or unknown), 'green',
    'cheap' (also when the green forecast isn't used), or 'mix' (balanced between them).
    Only claim "cheap and green" when both priorities picked (nearly) the same window."""
    if plan is None:
        return "both"
    if plan.green_ignored:
        return "cheap"
    eco, money = plan.eco_choice, plan.money_choice
    if eco is None or money is None or plan.priorities_agree:
        return "both"
    if plan.chosen.start == eco.start:
        return "green"
    if plan.chosen.start == money.start:
        return "cheap"
    return "mix"


def _now_is_best(plan: Plan, t) -> str:
    deadline = t(plan.deadline)
    match _best_at(plan):
        case "green":
            return _(
                "Charging now: right now is the greenest time before %(deadline)s.",
                deadline=deadline,
            )
        case "cheap":
            return _(
                "Charging now: right now is the cheapest time before %(deadline)s.",
                deadline=deadline,
            )
        case "mix":
            return _(
                "Charging now: right now is the best mix of green power and price before "
                "%(deadline)s.",
                deadline=deadline,
            )
    return _(
        "Charging now: right now is the cheapest and greenest time before %(deadline)s.",
        deadline=deadline,
    )


def _waiting_headline(plan: Plan | None) -> str:
    match _best_at(plan):
        case "green":
            return _("Waiting for green power")
        case "cheap":
            return _("Waiting for cheap power")
        case "mix":
            return _("Waiting for the best mix of green power and price")
    return _("Waiting for cheap, green power")


def _reason_sentence(plan: Plan, t) -> str:
    match plan.reason:
        case PlanReason.NOW_IS_BEST:
            return _now_is_best(plan, t)
        case PlanReason.WINDOW_STARTS_SOON:
            return _(
                "Charging now: the best window starts at %(start)s, only a few minutes "
                "away, so waiting isn't worth it.",
                start=t(plan.chosen.start),
            )
        case PlanReason.DEADLINE_TOO_CLOSE:
            return _(
                "Charging now: the battery needs about %(duration)s and should be ready "
                "at %(deadline)s. There isn't enough time for a cheaper window. Adjust "
                "the deadline rules if this happens often.",
                duration=fmt_duration(plan.duration),
                deadline=t(plan.deadline),
            )
        case PlanReason.NO_DATA:
            return _("Charging now: no prices are known yet for the time until your deadline.")
        case _:
            return _(
                "Charging in the planned window %(start)s–%(end)s.",
                start=t(plan.start),
                end=t(plan.end),
            )


def short_duration(d: timedelta) -> str:
    """For labels: '45 min' below an hour, else '1:30 h'."""
    minutes = max(0, round(d.total_seconds() / 60))
    if minutes < 60:
        return _("%(m)d min", m=minutes)
    return f"{minutes // 60}:{minutes % 60:02d} h"


def _duration_chip(cycle: Cycle, now: datetime) -> Chip | None:
    """How long it charged (so far), or, before it starts, the estimated charge time."""
    if cycle.actual_start is not None:
        charged = (cycle.end_at or now) - cycle.actual_start
        return Chip(_("%(d)s charged", d=short_duration(charged)))
    seconds = (cycle.plan or {}).get("duration_s")
    if seconds:
        return _estimate_chip(timedelta(seconds=seconds))
    return None


def _estimate_chip(duration: timedelta) -> Chip:
    return Chip(_("%(d)s estimated charge time", d=short_duration(duration)))


def cycle_chips(
    cycle: Cycle,
    tz: ZoneInfo,
    now: datetime,
    live=None,
    *,
    state: bool = True,
    expected_kwh: float | None = None,
):
    """Labels for one charge: why it started when it did, whether eco and cost agreed,
    energy, cost, green power and savings. The same on the status card (``state=False``:
    the headline already says "charging") and in the list of charges. ``live``: energy and
    cost so far of a running charge (``web.data.Live``). Before it starts: estimates,
    ``expected_kwh`` is the energy it will probably take."""
    chips: list[Chip] = []
    reason = (cycle.plan or {}).get("reason")
    running = cycle.end_at is None
    # how the charge started
    if cycle.override == "button_on":
        chips.append(Chip(_("Started with the plug button")))
    elif cycle.override == "charge_now":
        chips.append(Chip(_("Started in the app")))
    elif cycle.actual_start is None and running:
        until = (cycle.plan or {}).get("start")
        at_ = datetime.fromisoformat(until) if until else None
        chips.append(Chip(_("Waiting until %(t)s", t=fmt_day_time(at_, tz, now)), "info"))
    elif reason == PlanReason.DEADLINE_TOO_CLOSE:
        chips.append(Chip(_("Started right away: rule")))
    elif reason in (PlanReason.NOW_IS_BEST, PlanReason.WINDOW_STARTS_SOON):
        chips.append(Chip(_("Started right away: best window"), "good"))
    elif reason == PlanReason.NO_DATA:
        chips.append(Chip(_("Started right away: no forecast")))
    if cycle.actual_start is not None and cycle.actual_start - cycle.plugged_at >= timedelta(
        minutes=5
    ):
        waited = short_duration(cycle.actual_start - cycle.plugged_at)
        chips.append(Chip(_("Waited %(d)s", d=waited), "good"))
    plan = plan_from_dict(cycle.plan)
    if plan is not None and (agree := _agree_chip(plan)) is not None:
        chips.append(agree)
    if (duration := _duration_chip(cycle, now)) is not None:
        chips.append(duration)
    # state
    if state and running and cycle.actual_start is not None:
        chips.append(Chip(_("charging"), "info"))
    elif cycle.end_reason == "unplugged":
        chips.append(Chip(_("Battery taken off")))
    # numbers
    kwh = (live.kwh if live else None) if running else cycle.kwh
    cost = (live.cost_eur if live else None) if running else cycle.cost_actual_eur
    if kwh:
        chips.append(Chip(_("%(wh)s Wh", wh=fmt_num(kwh * 1000, "#,##0"))))
    if running and cycle.actual_start is None:
        if plan is not None:  # waiting: how it will probably go
            chips += _estimate_chips(plan, expected_kwh)
    elif running:
        if kwh and cost is not None:  # so far
            chips.append(Chip(_("Cost %(money)s", money=fmt_money(cost))))
    else:
        if kwh and cycle.cost_actual_eur is not None:
            change = _change(cycle.cost_actual_eur, cycle.cost_baseline_eur)
            chips.append(_cost_chip(cycle.cost_actual_eur, change, estimated=False))
        green = _change(cycle.ren_actual_pct, cycle.ren_baseline_pct)
        if (chip := _green_chip(green, estimated=False)) is not None:
            chips.append(chip)
        # only what was really saved: "0 g CO₂ avoided" would hide that it was more
        eur = (cycle.cost_baseline_eur or 0) - (cycle.cost_actual_eur or 0)
        co2 = (cycle.co2_baseline_g or 0) - (cycle.co2_actual_g or 0)
        money, grams = fmt_money(eur), fmt_num(co2, "#,##0")
        if eur >= 0.005 and co2 >= 1:
            text = _("%(money)s saved, %(grams)s g CO₂ avoided", money=money, grams=grams)
        elif eur >= 0.005:
            text = _("%(money)s saved", money=money)
        elif co2 >= 1:
            text = _("%(grams)s g CO₂ avoided", grams=grams)
        else:
            text = None
        if text:
            chips.append(Chip(text, "good"))
    return chips
