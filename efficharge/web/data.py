"""Read models for the pages: chart payloads, stats, rule warnings."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from efficharge.control.machine import DeviceState, Phase
from efficharge.control.planning import build_planner, planner_inputs, rule_from_row
from efficharge.control.remaining import CutoffModel, Remaining, estimate, learn
from efficharge.control.savings import intervals_from_samples, per_slot
from efficharge.control.serialize import plan_from_dict
from efficharge.db.models import (
    Cycle,
    DeadlineRuleRow,
    Device,
    DeviceAccount,
    DeviceSettings,
    EnergyDataRow,
    PowerSampleRow,
)
from efficharge.energy.series import SLOT, floor_slot
from efficharge.energy.store import load_series
from efficharge.planner.deadlines import matching_rule, resolve_deadline
from efficharge.planner.window import PlanReason
from efficharge.web.explain import recent_charge
from efficharge.web.i18n import _, fmt_day_time, fmt_num, get_locale, weekdays_text

#: plan reasons where the planner really picked a window (others mean "start now")
WINDOW_CHOSEN = {PlanReason.OPTIMAL, PlanReason.PARTIAL_DATA, PlanReason.WINDOW_STARTS_SOON}
#: a green "best window" band: also when the best window is right now (then it is the
#: same as "charged right away", so that gray band is left out)
WINDOW_SHOWN = {*WINDOW_CHOSEN, PlanReason.NOW_IS_BEST}
REMAINING_MIN = timedelta(minutes=5)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def chart_payload(
    db: AsyncSession,
    device: Device,
    state: DeviceState,
    cycle: Cycle | None,
    zone: str,
    tz: ZoneInfo,
    now: datetime,
) -> dict:
    """Everything the dashboard's three small-multiple charts need (one shared time axis).

    The charge in focus (planned, running, or the last one while the status card still
    shows it) is drawn as fixed at its plug-in: a "plugged in" line, "if charged right
    away" from the plug-in (gray), the chosen window (green, the part still ahead),
    charged so far (orange) and the expected rest (yellow), one after another. While
    nothing is plugged in, the window a plug-in now would get (the status text's preview)
    is drawn as an outline only, so it doesn't look like a planned charge."""
    focus, focus_plan = await _focus_charge(db, device.id, cycle, state, now)
    anchor = focus.plugged_at if focus is not None else now
    start = min(anchor, now) - timedelta(hours=2)
    # the deadline (and the rule behind it) is always shown, so the range must include it
    info = await deadline_info(db, device.id, state, tz, now)
    end = max(now + timedelta(hours=12), info["deadline"] + timedelta(hours=2))
    end = min(end, now + timedelta(hours=40))
    # which ends of the time axis move with the clock (see "live" in band())
    range_live = [
        start == now - timedelta(hours=2),
        end in (now + timedelta(hours=12), now + timedelta(hours=40)),
    ]
    series = await load_series(db, zone, start, end)

    def steps(values: dict[datetime, float], scale: float = 1.0) -> list[list[float]]:
        pts = [[_ms(t), round(v * scale, 2)] for t, v in sorted(values.items())]
        if pts:  # close the last step
            pts.append([pts[-1][0] + int(SLOT.total_seconds() * 1000), pts[-1][1]])
        return pts

    power_rows = (
        await db.execute(
            select(PowerSampleRow.ts, PowerSampleRow.watts)
            .where(PowerSampleRow.device_id == device.id, PowerSampleRow.ts >= start)
            .order_by(PowerSampleRow.ts)
        )
    ).all()

    bands: list[dict] = []
    lines: list[dict] = [{"x": _ms(now), "label": _("now"), "kind": "now"}]

    def band(kind: str, frm: datetime, to: datetime, label: str) -> None:
        if to > frm:
            # "live": the ends that are "now"; while a demo fast-forwards, the browser
            # moves them along with the clock between two refreshes
            live = [end for end, t in (("from", frm), ("to", to)) if t == now]
            bands.append(
                {"from": _ms(frm), "to": _ms(to), "label": label, "kind": kind, "live": live}
            )

    running = focus is not None and focus.end_at is None
    if focus is not None:
        when = fmt_day_time(focus.plugged_at, tz, now)
        lines.append(
            {"x": _ms(focus.plugged_at), "label": _("plugged in %(t)s", t=when), "kind": "plugin"}
        )
        if focus_plan is not None and focus_plan.reason in WINDOW_CHOSEN:
            # fixed at the plug-in: doesn't move with the clock or a re-plan
            band(
                "baseline",
                focus.plugged_at,
                focus.plugged_at + focus_plan.duration,
                _("if charged right away"),
            )
        rest_until = None
        if running and focus.actual_start is not None:
            remaining = await remaining_time(db, device.id, focus, state, now)
            if remaining is not None:
                # the same estimate as the status text; at least 5 min so it stays visible
                rest_until = now + max(remaining.left, REMAINING_MIN)
        if running and focus_plan is not None and focus_plan.reason in WINDOW_SHOWN:
            # the part of the window still ahead (after the expected rest)
            ahead = max(focus_plan.chosen.start, rest_until or now)
            band("plan", ahead, focus_plan.chosen.end, _("best window"))
        if focus.actual_start is not None:
            band("charging", focus.actual_start, focus.end_at or now, _("charging"))
        if rest_until is not None:
            band("remaining", now, rest_until, _("expected rest"))

    if cycle is None and state.phase in (Phase.IDLE, Phase.DONE):
        preview = await preview_plan(db, device.id, zone, now)
        band("next", preview.start, preview.end, _("best window if plugged in now"))

    label = f"{_('ready by')} {info['deadline'].astimezone(tz):%H:%M} · {info['short']}"
    lines.append({"x": _ms(info["deadline"]), "label": label, "kind": "deadline"})
    return {
        "locale": get_locale(),
        "timezone": str(tz),
        "now": _ms(now),
        "range": [_ms(start), _ms(end)],
        "range_live": range_live,
        "bands": bands,
        "lines": lines,
        "charts": [
            {
                "id": "green",
                "title": _("Green power share"),
                "unit": "%",
                "stepped": True,
                "points": steps(series.ren_share),
                "min": 0,
                "max": 100,
            },
            {
                "id": "price",
                "title": _("Electricity price"),
                "unit": "ct/kWh",
                "stepped": True,
                "points": steps(series.price, 0.1),
            },
            {
                "id": "power",
                "title": _("Charger power"),
                "unit": "W",
                "stepped": True,
                "points": [[_ms(ts), round(w, 1)] for ts, w in power_rows],
                "min": 0,
                # without a charge in view the axis would zoom into the standby noise
                "suggested_max": await usual_power(db, device.id),
            },
        ],
    }


@dataclass
class Totals:
    cycles: int
    kwh: float
    saved_eur: float | None
    avoided_co2_kg: float | None
    avg_duration: timedelta | None


def _users_charges(user_id: int):
    """The user's charges for the statistics: those of a virtual plug only while the
    user has no real plug (trying it out must not distort the real numbers)."""
    plug, account = aliased(Device), aliased(DeviceAccount)  # not the outer query's rows
    real_plug = exists().where(
        plug.user_id == user_id, account.id == plug.account_id, account.kind != "simulated"
    )
    return (
        select(Cycle, Device.name)
        .join(Device, Device.id == Cycle.device_id)
        .join(DeviceAccount, DeviceAccount.id == Device.account_id)
        .where(
            Device.user_id == user_id,
            Cycle.deleted_at.is_(None),
            or_(DeviceAccount.kind != "simulated", ~real_plug),
        )
    )


async def finished_cycles(db: AsyncSession, user_id: int, limit: int | None = None):
    q = _users_charges(user_id).where(Cycle.end_at.is_not(None)).order_by(Cycle.end_at.desc())
    if limit:
        q = q.limit(limit)
    return (await db.execute(q)).all()


async def recent_cycles(db: AsyncSession, user_id: int, limit: int = 50):
    """Newest first, including a charge that is planned or running right now."""
    q = _users_charges(user_id).order_by(Cycle.plugged_at.desc()).limit(limit)
    return (await db.execute(q)).all()


@dataclass(frozen=True)
class Live:
    """A running charge so far."""

    kwh: float
    cost_eur: float | None  # exchange price × energy, None while prices are missing


async def live_numbers(db: AsyncSession, cycle: Cycle, zone: str, now: datetime) -> Live | None:
    """Energy and cost of a running charge, from the power samples up to now."""
    if cycle.actual_start is None:
        return None
    rows = (
        await db.execute(
            select(PowerSampleRow.ts, PowerSampleRow.watts).where(
                PowerSampleRow.device_id == cycle.device_id, PowerSampleRow.ts >= cycle.actual_start
            )
        )
    ).all()
    intervals = intervals_from_samples([(t, w) for t, w in rows], end=now)
    series = await load_series(db, zone, cycle.actual_start - SLOT, now + SLOT)
    total = series.weighted_sum(series.price, per_slot(intervals))  # EUR/MWh × kWh
    return Live(sum(iv.kwh for iv in intervals), total / 1000 if total is not None else None)


def totals(cycles: list[Cycle]) -> Totals:
    priced = [
        c for c in cycles if c.cost_actual_eur is not None and c.cost_baseline_eur is not None
    ]
    co2 = [c for c in cycles if c.co2_actual_g is not None and c.co2_baseline_g is not None]
    durations = [
        c.end_at - c.actual_start
        for c in cycles
        if c.end_reason == "full" and c.actual_start and c.end_at
    ]
    return Totals(
        cycles=len(cycles),
        kwh=sum(c.kwh or 0 for c in cycles),
        saved_eur=sum(c.cost_baseline_eur - c.cost_actual_eur for c in priced) if priced else None,
        avoided_co2_kg=sum(c.co2_baseline_g - c.co2_actual_g for c in co2) / 1000 if co2 else None,
        avg_duration=(
            timedelta(seconds=statistics.median(d.total_seconds() for d in durations))
            if durations
            else None
        ),
    )


def cumulative(cycles: list[Cycle]) -> dict:
    """Running totals over time for the stats charts (oldest first)."""
    eur, co2 = 0.0, 0.0
    eur_pts, co2_pts = [], []
    for c in sorted(cycles, key=lambda c: c.end_at):
        if c.cost_actual_eur is not None and c.cost_baseline_eur is not None:
            eur += c.cost_baseline_eur - c.cost_actual_eur
            eur_pts.append([_ms(c.end_at), round(eur, 3)])
        if c.co2_actual_g is not None and c.co2_baseline_g is not None:
            co2 += (c.co2_baseline_g - c.co2_actual_g) / 1000
            co2_pts.append([_ms(c.end_at), round(co2, 3)])
    ends = [_ms(c.end_at) for c in cycles]
    pad = max(12 * 3600 * 1000, (max(ends) - min(ends)) // 20) if ends else 0
    return {
        "locale": get_locale(),
        # x axis fitted to the data (a single charge must not stretch it over months)
        "xrange": [min(ends) - pad, max(ends) + pad] if ends else None,
        "charts": [
            {
                "id": "eur",
                "title": _("Money saved (total)"),
                "unit": "€",
                "points": eur_pts,
                "stepped": True,
            },
            {
                "id": "co2",
                "title": _("CO₂ avoided (total)"),
                "unit": "kg",
                "points": co2_pts,
                "stepped": True,
            },
        ],
    }


# ── deadline rules ──


def _minutes(hhmm: str) -> int:
    t = time.fromisoformat(hhmm)
    return t.hour * 60 + t.minute


def _ranges(rule: DeadlineRuleRow) -> list[tuple[int, int]]:
    a, b = _minutes(rule.plugged_from), _minutes(rule.plugged_until)
    if a == b:
        return [(0, 1440)]
    return [(a, b)] if a < b else [(a, 1440), (0, b)]


def rule_overlaps(rules: list[DeadlineRuleRow]) -> list[tuple[int, int, list[int]]]:
    """Pairs of rules (by position, 1-based) that both match some plug-in times, with the
    shared weekdays. The rule listed first wins there, so the user should know."""
    out = []
    for i, r1 in enumerate(rules):
        for j in range(i + 1, len(rules)):
            r2 = rules[j]
            days = [d for d in range(7) if r1.weekdays >> d & 1 and r2.weekdays >> d & 1]
            if not days:
                continue
            if any(a1 < b2 and a2 < b1 for a1, b1 in _ranges(r1) for a2, b2 in _ranges(r2)):
                out.append((i + 1, j + 1, days))
    return out


async def device_rules(db: AsyncSession, device_id: int) -> list[DeadlineRuleRow]:
    return list(
        (
            await db.scalars(
                select(DeadlineRuleRow)
                .where(DeadlineRuleRow.device_id == device_id)
                .order_by(DeadlineRuleRow.priority.desc(), DeadlineRuleRow.position)
            )
        ).all()
    )


# ── which rule set the deadline ──


def within_text(minutes: int) -> str:
    """'ready within 6 h'"""
    return _("ready within %(h)s h", h=fmt_num(minutes / 60, "0.#"))


def rule_target(row: DeadlineRuleRow) -> str:
    """'ready by 18:00' or 'ready within 6 h'"""
    if row.within_min:
        return within_text(row.within_min)
    return _("ready by %(t)s", t=row.ready_by)


def rule_text(row: DeadlineRuleRow) -> str:
    """Short: '08:00–15:00 → ready by 18:00', with the days only if not every day."""
    parts = []
    if row.weekdays != 127:
        parts.append(weekdays_text(row.weekdays))
    if row.plugged_from != row.plugged_until:
        parts.append(f"{row.plugged_from}–{row.plugged_until}")
    when = ", ".join(parts) or _("Any time")
    return f"{when} → {rule_target(row)}"


def fallback_text(minutes: int) -> str:
    return f"{_('Otherwise')} → {within_text(minutes)}"


#: the power axis before this plug has charged anything
DEFAULT_POWER_AXIS_W = 50.0


async def usual_power(db: AsyncSession, device_id: int) -> float:
    """The power this plug normally draws while charging: the highest reading of its
    past charges (no charge yet: 50 W). The chart's power axis reaches at least this."""
    peak = await db.scalar(
        select(func.max(PowerSampleRow.watts))
        .join(
            Cycle,
            (Cycle.device_id == PowerSampleRow.device_id)
            & (PowerSampleRow.ts >= Cycle.actual_start)
            & (PowerSampleRow.ts <= func.coalesce(Cycle.end_at, PowerSampleRow.ts)),
        )
        .where(PowerSampleRow.device_id == device_id, Cycle.deleted_at.is_(None))
    )
    return peak if peak and peak > DEFAULT_POWER_AXIS_W / 10 else DEFAULT_POWER_AXIS_W


async def _focus_charge(
    db: AsyncSession, device_id: int, cycle: Cycle | None, state: DeviceState, now: datetime
):
    """The charge the chart is about: the running one (with the live plan), or the one
    the status card still shows (with the plan it had). (None, None) otherwise."""
    if cycle is not None:
        return cycle, state.plan or _stored_plan(cycle)
    last = await db.scalar(
        select(Cycle)
        .where(Cycle.device_id == device_id, Cycle.end_at.is_not(None), Cycle.deleted_at.is_(None))
        .order_by(Cycle.end_at.desc())
        .limit(1)
    )
    if recent_charge(last, now) is None:
        return None, None
    return last, _stored_plan(last)


def _stored_plan(cycle: Cycle):
    """The plan saved with a charge; None if missing or incomplete (e.g. a restored row)."""
    try:
        return plan_from_dict(cycle.plan)
    except (KeyError, TypeError, ValueError):
        return None


async def deadline_info(
    db: AsyncSession, device_id: int, state: DeviceState, tz: ZoneInfo, now: datetime
) -> dict:
    """Why the deadline is what it is: a rule, a one-off "ready by", or the fallback rule.

    While a charge is planned or running, that charge's deadline. Otherwise the deadline
    a plug-in *now* would get, so the rule in force is always visible.
    ``short`` labels the deadline line in the chart; ``text`` (the rule itself) and
    ``href`` (edit it) go below the status."""
    settings_url = f"/devices/{device_id}/settings"
    active = (
        state.plan is not None
        and state.plugged_at is not None
        and state.phase in (Phase.PLANNED, Phase.CHARGING, Phase.PAUSED_BY_USER)
    )
    plugged_at = state.plugged_at if active else now
    if active and state.override_deadline is not None:
        return {
            "deadline": state.plan.deadline,
            "short": _("one-off"),
            "text": _("Once: ready by %(t)s", t=fmt_day_time(state.plan.deadline, tz, now)),
            "href": None,
        }
    rows = await device_rules(db, device_id)
    rules = [rule_from_row(r) for r in rows]
    settings = await db.get(DeviceSettings, device_id)
    fallback_min = settings.fallback_within_min if settings else 720
    index = matching_rule(plugged_at, rules, tz)
    deadline = (
        state.plan.deadline
        if active
        else resolve_deadline(plugged_at, rules, tz, default=timedelta(minutes=fallback_min))
    )
    if index is None:
        info = {
            "short": _("default rule"),
            "text": fallback_text(fallback_min),
            "href": f"{settings_url}?edit=fallback#rule-fallback",
        }
    else:
        row = rows[index]
        info = {
            "short": _("rule %(n)d", n=index + 1),
            "text": rule_text(row),
            "href": f"{settings_url}?edit={row.id}#rule-{row.id}",
        }
    return {"deadline": deadline, **info}


# ── remaining charge time ──

#: device id → (id of the newest full charge, learned tail); re-learned after a new charge
_cutoff_models: dict[int, tuple[int | None, CutoffModel | None]] = {}
LEARN_FROM = 8  # the last full charges


async def _samples(db: AsyncSession, device_id: int, start: datetime, end: datetime | None):
    q = select(PowerSampleRow.ts, PowerSampleRow.watts).where(
        PowerSampleRow.device_id == device_id, PowerSampleRow.ts >= start
    )
    if end is not None:
        q = q.where(PowerSampleRow.ts <= end)
    return [(t, w) for t, w in (await db.execute(q.order_by(PowerSampleRow.ts))).all()]


async def cutoff_model(db: AsyncSession, device_id: int) -> CutoffModel | None:
    full = (
        await db.scalars(
            select(Cycle)
            .where(
                Cycle.device_id == device_id,
                Cycle.end_reason == "full",
                Cycle.deleted_at.is_(None),
                Cycle.actual_start.is_not(None),
                Cycle.end_at.is_not(None),
            )
            .order_by(Cycle.end_at.desc())
            .limit(LEARN_FROM)
        )
    ).all()
    newest = full[0].id if full else None
    cached = _cutoff_models.get(device_id)
    if cached is not None and cached[0] == newest:
        return cached[1]
    charges = [(await _samples(db, device_id, c.actual_start, c.end_at), c.end_at) for c in full]
    model = learn(charges)
    _cutoff_models[device_id] = (newest, model)
    return model


async def remaining_time(
    db: AsyncSession, device_id: int, cycle: Cycle, state: DeviceState, now: datetime
) -> Remaining | None:
    """Estimated time left for a running charge (see control.remaining)."""
    if cycle.actual_start is None or state.phase is not Phase.CHARGING:
        return None
    samples = await _samples(db, device_id, cycle.actual_start, None)
    return estimate(
        samples,
        now,
        await cutoff_model(db, device_id),
        started=cycle.actual_start,
        planned=state.plan.duration if state.plan else None,
    )


# ── preview: the best window if the battery were plugged in now ──

#: device id → (cache key, plan). Recomputed only when the inputs change (see below).
_previews: dict[int, tuple[tuple, object]] = {}


async def preview_plan(db: AsyncSession, device_id: int, zone: str, now: datetime):
    """The plan a plug-in right now would get (for the chart while nothing charges).

    Cached until new energy data arrives, any planner input changes (rules, settings,
    past charges: ``PlannerInputs``), or the next 15-minute slot begins (a window
    starting "now" moves along with the clock)."""
    key = (
        floor_slot(now),
        await db.scalar(select(func.max(EnergyDataRow.fetched_at))),
        await planner_inputs(db, device_id),
    )
    cached = _previews.get(device_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    make_plan = await build_planner(db, device_id, zone, now)
    plan = make_plan(now, None, now)
    _previews[device_id] = (key, plan)
    return plan
