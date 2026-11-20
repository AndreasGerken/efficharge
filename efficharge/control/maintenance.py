"""Background job: settle finished charges and keep the database small.

Runs every few hours; every step is idempotent, so a missed or repeated run is harmless.

- **Settle:** a charge's € and CO₂ are computed when it ends, partly from forecasts
  (CO₂ actuals arrive later). Once the energy data of its span can no longer change,
  they are computed once more and the charge is marked ``finalised``.
- **Prune:** power readings outside of charges (the plug idling) are only needed for
  the recent charts. After ``KEEP_IDLE_SAMPLES`` they are deleted. Readings during a
  charge stay: the learned switch-off power, the usual power and the savings use them.
  SQLite reuses the freed pages, so no VACUUM is needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from efficharge.control.store import DbStateStore
from efficharge.db.models import Cycle, PowerSampleRow
from efficharge.energy.fetcher import REFETCH_PAST

logger = logging.getLogger(__name__)

#: the fetcher re-fetches the last day; older energy data is final
SETTLED_AFTER = REFETCH_PAST + timedelta(hours=6)
KEEP_IDLE_SAMPLES = timedelta(days=90)
INTERVAL = timedelta(hours=6)


async def settle_cycles(store: DbStateStore, now: datetime) -> int:
    sessions = store.sessions
    async with sessions() as session:
        ids = (
            await session.scalars(
                select(Cycle.id).where(
                    Cycle.end_at.is_not(None),
                    Cycle.end_at < now - SETTLED_AFTER,
                    Cycle.finalised.is_(False),
                )
            )
        ).all()
    for cycle_id in ids:  # one short write transaction each
        async with sessions() as session, session.begin():
            cycle = await session.get(Cycle, cycle_id)
            await store.update_savings(session, cycle)
            cycle.finalised = True
    return len(ids)


async def prune_samples(sessions: async_sessionmaker, now: datetime) -> int:
    during_a_charge = exists().where(
        Cycle.device_id == PowerSampleRow.device_id,
        PowerSampleRow.ts >= Cycle.plugged_at,
        PowerSampleRow.ts <= func.coalesce(Cycle.end_at, PowerSampleRow.ts),
    )
    async with sessions() as session, session.begin():
        result = await session.execute(
            delete(PowerSampleRow).where(
                PowerSampleRow.ts < now - KEEP_IDLE_SAMPLES, ~during_a_charge
            )
        )
    return result.rowcount


async def maintain(store: DbStateStore, now: datetime) -> None:
    settled = await settle_cycles(store, now)
    pruned = await prune_samples(store.sessions, now)
    if settled or pruned:
        logger.info("maintenance: %d charges settled, %d idle readings deleted", settled, pruned)


async def run(store: DbStateStore, clock: Callable[[], datetime]) -> None:
    while True:
        try:
            await maintain(store, clock())
        except Exception:
            logger.exception("maintenance crashed")
        await asyncio.sleep(INTERVAL.total_seconds())
