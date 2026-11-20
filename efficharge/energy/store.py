"""Energy data in the DB: always stored expanded to the 15-min grid."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from efficharge.db.models import EnergyDataRow, utcnow
from efficharge.energy.client import RawEnergyData
from efficharge.energy.series import EnergySeries, expand_steps


async def known_price_until(session: AsyncSession, zone: str) -> datetime | None:
    return await session.scalar(
        select(func.max(EnergyDataRow.ts)).where(
            EnergyDataRow.zone == zone, EnergyDataRow.price_eur_mwh.is_not(None)
        )
    )


async def known_ren_share_until(session: AsyncSession, zone: str) -> datetime | None:
    return await session.scalar(
        select(func.max(EnergyDataRow.ts)).where(
            EnergyDataRow.zone == zone, EnergyDataRow.ren_share_pct.is_not(None)
        )
    )


async def upsert(session: AsyncSession, zone: str, raw: RawEnergyData) -> bool:
    """Store new/changed slots. Returns True if the known horizon of prices or of the
    green-share forecast grew (new data published → planned devices should replan)."""
    before = (await known_price_until(session, zone), await known_ren_share_until(session, zone))
    price, ren, co2 = expand_steps(raw.price), expand_steps(raw.ren_share), expand_steps(raw.co2)
    now = utcnow()
    rows = [
        {
            "zone": zone,
            "ts": ts,
            "price_eur_mwh": price.get(ts),
            "ren_share_pct": ren.get(ts),
            "co2_g_kwh": co2.get(ts),
            "fetched_at": now,
        }
        for ts in sorted(price.keys() | ren.keys() | co2.keys())
    ]
    if rows:
        stmt = sqlite_insert(EnergyDataRow)
        # A value that is missing in this response keeps the stored one.
        stmt = stmt.on_conflict_do_update(
            index_elements=["zone", "ts"],
            set_={
                col: func.coalesce(stmt.excluded[col], getattr(EnergyDataRow, col))
                for col in ("price_eur_mwh", "ren_share_pct", "co2_g_kwh")
            }
            | {"fetched_at": stmt.excluded.fetched_at},
        )
        # Bind through the ORM type so datetimes are stored as naive UTC.
        for chunk_start in range(0, len(rows), 500):
            await session.execute(stmt, rows[chunk_start : chunk_start + 500])
    after = (await known_price_until(session, zone), await known_ren_share_until(session, zone))
    return any(a is not None and (b is None or a > b) for a, b in zip(after, before, strict=True))


async def load_series(
    session: AsyncSession, zone: str, start: datetime, end: datetime
) -> EnergySeries:
    result = await session.execute(
        select(EnergyDataRow).where(
            EnergyDataRow.zone == zone, EnergyDataRow.ts >= start, EnergyDataRow.ts < end
        )
    )
    price: dict[datetime, float] = {}
    ren: dict[datetime, float] = {}
    co2: dict[datetime, float] = {}
    for row in result.scalars():
        if row.price_eur_mwh is not None:
            price[row.ts] = row.price_eur_mwh
        if row.ren_share_pct is not None:
            ren[row.ts] = row.ren_share_pct
        if row.co2_g_kwh is not None:
            co2[row.ts] = row.co2_g_kwh
    return EnergySeries(price=price, ren_share=ren, co2=co2)
