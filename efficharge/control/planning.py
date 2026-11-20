"""Builds the per-device inputs of the pure core from the DB:
machine ``Settings`` and the ``make_plan`` closure (energy data, rules, history)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from efficharge.control.machine import Planner, Settings
from efficharge.db.models import Cycle, DeadlineRuleRow, Device, DeviceSettings, User
from efficharge.energy.store import load_series
from efficharge.planner.deadlines import DeadlineRule, resolve_deadline
from efficharge.planner.duration import estimate_duration
from efficharge.planner.window import Plan, Weights, best_window

HISTORY = 20
HORIZON = timedelta(days=2)


def _t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def rule_from_row(row: DeadlineRuleRow) -> DeadlineRule:
    return DeadlineRule(
        ready_by=_t(row.ready_by) if row.ready_by else None,
        within=timedelta(minutes=row.within_min) if row.within_min else None,
        weekdays=frozenset(d for d in range(7) if row.weekdays >> d & 1),
        plugged_from=_t(row.plugged_from),
        plugged_until=_t(row.plugged_until),
        priority=row.priority,
    )


def machine_settings(s: DeviceSettings) -> Settings:
    threshold = s.threshold_w
    if s.standby_w is not None:
        threshold = max(threshold, s.standby_w + 2.0)
    return Settings(threshold_w=threshold, metered=s.metered)


async def _full_charges(session: AsyncSession, device_id: int):
    """The last charges that ended because the battery was full (newest first)."""
    return (
        await session.execute(
            select(Cycle.actual_start, Cycle.end_at, Cycle.kwh)
            .where(
                Cycle.device_id == device_id,
                Cycle.end_reason == "full",
                Cycle.deleted_at.is_(None),  # hidden by the user
                # paused cycles would count the pause as charge time
                Cycle.override.is_(None) | Cycle.override.not_in(["button_off", "stop"]),
                Cycle.actual_start.is_not(None),
            )
            .order_by(Cycle.end_at.desc())
            .limit(HISTORY)
        )
    ).all()


async def past_durations(session: AsyncSession, device_id: int) -> list[timedelta]:
    """Durations of the last full charges (oldest first)."""
    rows = await _full_charges(session, device_id)
    return [end - start for start, end, _kwh in reversed(rows)]


async def expected_kwh(session: AsyncSession, device_id: int, duration: timedelta) -> float | None:
    """Energy of the next charge: the battery size for plugs without metering, else the
    average power of the same past charges the duration is learned from × ``duration``.
    None without history."""
    settings = await session.get(DeviceSettings, device_id)
    if settings is not None and not settings.metered:
        return settings.battery_kwh
    rows = [(end - start, kwh) for start, end, kwh in await _full_charges(session, device_id)]
    hours = sum(d.total_seconds() for d, kwh in rows if kwh) / 3600
    if hours <= 0:
        return None
    return sum(kwh for _d, kwh in rows if kwh) / hours * duration.total_seconds() / 3600


@dataclass(frozen=True)
class PlannerInputs:
    """Everything a plan depends on besides the energy data. Hashable: the preview cache
    uses it as its key, so a cached plan can't miss a changed rule or setting."""

    rules: tuple[DeadlineRule, ...]
    fallback: timedelta  # the default rule: ready within this long
    tz: ZoneInfo
    duration: timedelta
    weights: Weights


async def planner_inputs(session: AsyncSession, device_id: int) -> PlannerInputs:
    device = await session.get(Device, device_id)
    if device is None:
        raise LookupError(f"device {device_id} not found")
    settings = await session.get(DeviceSettings, device_id)
    user = await session.get(User, device.user_id)
    rule_rows = (
        await session.scalars(
            select(DeadlineRuleRow)
            .where(DeadlineRuleRow.device_id == device_id)
            .order_by(DeadlineRuleRow.position)
        )
    ).all()
    user_estimate = timedelta(minutes=settings.default_duration_min if settings else 180)
    return PlannerInputs(
        rules=tuple(rule_from_row(r) for r in rule_rows),
        fallback=timedelta(minutes=settings.fallback_within_min if settings else 720),
        tz=ZoneInfo(user.timezone if user else "Europe/Berlin"),
        duration=estimate_duration(await past_durations(session, device_id), user_estimate),
        weights=Weights.from_slider(user.price_weight if user else 0.5),
    )


async def build_planner(session: AsyncSession, device_id: int, zone: str, now: datetime) -> Planner:
    i = await planner_inputs(session, device_id)
    series = await load_series(session, zone, now - timedelta(hours=1), now + HORIZON)

    def make_plan(plugged_at: datetime, override: datetime | None, now: datetime) -> Plan:
        deadline = resolve_deadline(
            plugged_at, i.rules, i.tz, override=override, default=i.fallback
        )
        return best_window(
            series, now=now, deadline=deadline, duration=i.duration, weights=i.weights
        )

    return make_plan
