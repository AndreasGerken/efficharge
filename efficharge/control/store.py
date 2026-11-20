"""DB implementation of the runner's ``StateStore``: state + cycle bookkeeping."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from efficharge.control.machine import (
    Action,
    DeviceState,
    FinishCycle,
    MarkCharging,
    Phase,
    RecordOverride,
    SavePlan,
    StartCycle,
)
from efficharge.control.savings import compute_savings, flat_profile, intervals_from_samples
from efficharge.control.serialize import plan_to_dict, state_from_dict, state_to_dict
from efficharge.db.models import Cycle, DeviceSettings, DeviceStateRow, PowerSampleRow, utcnow
from efficharge.db.session import cancel_safe, readers_for
from efficharge.devices.base import PowerReading
from efficharge.energy.store import load_series

#: phases in which the plug should be on (``should_charge_now``, for the planned API)
_ON_PHASES = {Phase.IDLE, Phase.CHARGING, Phase.DONE}


class DbStateStore:
    def __init__(self, sessions: async_sessionmaker, zone: str) -> None:
        self.sessions = sessions
        self.zone = zone

    async def load(self, device_id: str) -> DeviceState:
        async with readers_for(self.sessions)() as session:
            row = await session.get(DeviceStateRow, int(device_id))
            return state_from_dict(row.data if row else None)

    @cancel_safe  # a cancelled runner must never leave half a transaction (and the lock)
    async def save(self, device_id: str, state: DeviceState, actions: list[Action]) -> None:
        dev = int(device_id)
        async with self.sessions() as session, session.begin():
            row = await session.get(DeviceStateRow, dev)
            if row is None:
                row = DeviceStateRow(device_id=dev)
                session.add(row)
            for action in actions:
                await self._apply(session, dev, row, action)
            row.phase = state.phase.value
            row.data = state_to_dict(state)
            row.should_charge_now = state.phase in _ON_PHASES
            row.updated_at = utcnow()

    @cancel_safe
    async def record_power(self, device_id: str, reading: PowerReading) -> None:
        dev = int(device_id)
        async with self.sessions() as session, session.begin():
            session.add(PowerSampleRow(device_id=dev, ts=reading.timestamp, watts=reading.watts))
            row = await session.get(DeviceStateRow, dev)
            if row is None:
                row = DeviceStateRow(device_id=dev, data={})
                session.add(row)
            row.last_power_w = reading.watts
            row.last_seen = reading.timestamp

    # ── bookkeeping ──

    async def _apply(
        self, session: AsyncSession, dev: int, row: DeviceStateRow, action: Action
    ) -> None:
        cycle = await session.get(Cycle, row.current_cycle_id) if row.current_cycle_id else None
        match action:
            case StartCycle(plugged_at=plugged_at):
                cycle = Cycle(device_id=dev, plugged_at=plugged_at)
                session.add(cycle)
                await session.flush()
                row.current_cycle_id = cycle.id
            case SavePlan(plan=plan) if cycle is not None:
                cycle.plan = plan_to_dict(plan)
                cycle.deadline = plan.deadline
                cycle.planned_start, cycle.planned_end = plan.start, plan.end
            case RecordOverride(kind=kind, at=at) if cycle is not None:
                cycle.override, cycle.override_at = kind.value, at
            case MarkCharging(at=at) if cycle is not None:
                if cycle.actual_start is None:
                    cycle.actual_start = at
            case FinishCycle(reason=reason, at=at) if cycle is not None:
                cycle.end_at, cycle.end_reason = at, reason.value
                await self.update_savings(session, cycle)
                row.current_cycle_id = None

    async def update_savings(self, session: AsyncSession, cycle: Cycle) -> None:
        """(Re)compute kWh, € and CO₂ for a finished cycle. Also used by the nightly
        job that finalises cycles once actual CO₂ values exist."""
        if cycle.end_at is None or cycle.actual_start is None:
            return
        samples = (
            await session.execute(
                select(PowerSampleRow.ts, PowerSampleRow.watts).where(
                    PowerSampleRow.device_id == cycle.device_id,
                    PowerSampleRow.ts >= cycle.actual_start,
                    PowerSampleRow.ts < cycle.end_at,
                )
            )
        ).all()
        intervals = intervals_from_samples([(ts, w) for ts, w in samples], end=cycle.end_at)
        if not intervals:
            settings = await session.get(DeviceSettings, cycle.device_id)
            if settings is None or not settings.battery_kwh:
                return
            duration = max(cycle.end_at - cycle.actual_start, timedelta(minutes=1))
            intervals = flat_profile(cycle.actual_start, duration, settings.battery_kwh)
        baseline_start = cycle.plugged_at
        span_start = min(cycle.actual_start, baseline_start)
        span_end = max(cycle.end_at, baseline_start + (cycle.end_at - cycle.actual_start))
        series = await load_series(
            session, self.zone, span_start - timedelta(minutes=15), span_end + timedelta(minutes=15)
        )
        result = compute_savings(
            intervals,
            actual_start=cycle.actual_start,
            baseline_start=baseline_start,
            series=series,
        )
        cycle.kwh = result.kwh
        cycle.cost_actual_eur = result.cost_actual_eur
        cycle.cost_baseline_eur = result.cost_baseline_eur
        cycle.co2_actual_g = result.co2_actual_g
        cycle.co2_baseline_g = result.co2_baseline_g
        cycle.ren_actual_pct = result.ren_actual_pct
        cycle.ren_baseline_pct = result.ren_baseline_pct
