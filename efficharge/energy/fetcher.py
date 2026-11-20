"""Background job: fetch energy data for everybody, every 15 minutes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from efficharge.energy import store
from efficharge.energy.client import EnergyChartsClient, EnergyDataError

logger = logging.getLogger(__name__)

#: re-fetch the last day too: CO₂ actuals replace forecasts there
REFETCH_PAST = timedelta(days=1)


class EnergyFetcher:
    def __init__(
        self,
        client: EnergyChartsClient,
        sessions: async_sessionmaker,
        zone: str,
        clock: Callable[[], datetime],
        *,
        on_new_prices: Callable[[], Awaitable[None]] | None = None,
        interval: timedelta = timedelta(minutes=15),
        past: timedelta = REFETCH_PAST,
    ) -> None:
        self.client = client
        self.sessions = sessions
        self.zone = zone
        self.clock = clock
        self.on_new_prices = on_new_prices
        self.interval = interval
        self.past = past
        self.last_success: datetime | None = None

    async def fetch_once(self) -> bool:
        raw = await self.client.fetch_window(self.clock(), past=self.past)
        async with self.sessions() as session, session.begin():
            extended = await store.upsert(session, self.zone, raw)
        self.last_success = self.clock()
        if extended:
            logger.info("new prices published for %s", self.zone)
            if self.on_new_prices is not None:
                await self.on_new_prices()
        return extended

    async def run(self) -> None:
        while True:
            try:
                await self.fetch_once()
            except EnergyDataError as e:
                logger.warning("energy data fetch failed: %s", e)
            except Exception:
                logger.exception("energy data fetch crashed")
            await asyncio.sleep(self.interval.total_seconds())
