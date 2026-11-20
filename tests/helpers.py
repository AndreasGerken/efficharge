from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from efficharge.energy.series import SLOT, EnergySeries

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def at(hours: float = 0, minutes: float = 0) -> datetime:
    return T0 + timedelta(hours=hours, minutes=minutes)


def make_series(
    prices: Sequence[float | None],
    ren: Sequence[float | None] | None = None,
    start: datetime = T0,
    step: timedelta = SLOT,
) -> EnergySeries:
    """Series with one value per ``step`` from ``start``. ``ren`` defaults to 50 %."""
    ren = ren if ren is not None else [50.0] * len(prices)
    return EnergySeries.from_points(
        {start + i * step: p for i, p in enumerate(prices)},
        {start + i * step: r for i, r in enumerate(ren)},
    )
