"""Energy data as a step function on a fixed 15-minute UTC grid.

Day-ahead prices are constant within each market time unit (15 min since
1 Oct 2025, 1 h before). Interpolating them (the first prototype used a cubic spline) invents
prices that never existed, so this module only ever expands steps:
an hourly value becomes four equal quarter-hours.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SLOT = timedelta(minutes=15)
#: Points further apart than this are treated as a gap, not as one long step.
MAX_STEP = timedelta(hours=1)


def floor_slot(dt: datetime) -> datetime:
    """Round an aware datetime down to the start of its 15-min slot (in UTC)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware datetimes")
    dt = dt.astimezone(UTC)
    return dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)


def ceil_slot(dt: datetime) -> datetime:
    floored = floor_slot(dt)
    return floored if floored == dt else floored + SLOT


def expand_steps(points: Mapping[datetime, float | None]) -> dict[datetime, float]:
    """Expand ``{start_of_step: value}`` to ``{slot_start: value}`` on the 15-min grid.

    Each point is valid until the next point. If the next point is more than
    ``MAX_STEP`` away (a gap) or missing, the point lasts as long as the step
    before it. This handles mixed hourly + 15-min history correctly.
    ``None`` values are dropped, so they become gaps.
    """
    items = sorted((floor_slot(t), v) for t, v in points.items())
    if not items:
        return {}
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(items) if b[0] > a[0]]
    native = min(gaps, default=SLOT)
    out: dict[datetime, float] = {}
    step = native
    for i, (start, value) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else None
        # A point lasts until the next one. Before a gap (or at the end) it lasts as
        # long as the previous step, i.e. the resolution the data had at that point.
        if nxt is not None and nxt - start <= MAX_STEP:
            step = nxt - start
        if value is None:
            continue
        end = start + step
        t = start
        while t < end:
            out[t] = float(value)
            t += SLOT
    return out


@dataclass(frozen=True)
class EnergySeries:
    """Price (EUR/MWh) and renewable share (%) per 15-min slot.

    Either value of a slot may be ``None`` (not published yet / gap).
    """

    price: Mapping[datetime, float]
    ren_share: Mapping[datetime, float]
    #: actual or forecast grid CO2 intensity in gCO2eq/kWh, used for savings stats
    co2: Mapping[datetime, float] | None = None

    @classmethod
    def from_points(
        cls,
        price: Mapping[datetime, float | None],
        ren_share: Mapping[datetime, float | None],
        co2: Mapping[datetime, float | None] | None = None,
    ) -> EnergySeries:
        return cls(
            price=expand_steps(price),
            ren_share=expand_steps(ren_share),
            co2=expand_steps(co2) if co2 is not None else None,
        )

    def slots(self, start: datetime, end: datetime) -> Iterable[datetime]:
        t = floor_slot(start)
        while t < end:
            yield t
            t += SLOT

    def known_until(self, *, need_price: bool, need_ren: bool, after: datetime) -> datetime:
        """End of the contiguous run of slots, starting at ``after``, with the needed data."""
        t = floor_slot(after)
        while (not need_price or t in self.price) and (not need_ren or t in self.ren_share):
            t += SLOT
        return max(t, after)

    def average(
        self, values: Mapping[datetime, float], start: datetime, end: datetime
    ) -> float | None:
        """Time-weighted mean of a step series over ``[start, end)``.

        Slots without data are skipped. Returns ``None`` if no slot has data.
        """
        total = 0.0
        covered = 0.0
        for slot in self.slots(start, end):
            if slot not in values:
                continue
            overlap = (min(end, slot + SLOT) - max(start, slot)).total_seconds()
            total += values[slot] * overlap
            covered += overlap
        return total / covered if covered else None

    def weighted_sum(
        self, values: Mapping[datetime, float], energy_per_slot: Mapping[datetime, float]
    ) -> float | None:
        """Σ value(slot) × energy(slot). ``None`` if any slot with energy has no value."""
        total = 0.0
        for slot, kwh in energy_per_slot.items():
            if kwh == 0:
                continue
            if slot not in values:
                return None
            total += values[slot] * kwh
        return total


def from_unix(
    seconds: Iterable[int], values: Iterable[float | None]
) -> dict[datetime, float | None]:
    """Turn the energy-charts ``unix_seconds`` + values lists into a mapping."""
    return {datetime.fromtimestamp(s, tz=UTC): v for s, v in zip(seconds, values, strict=True)}
