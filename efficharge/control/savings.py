"""€ and CO₂ of a cycle compared with its baseline ("charging right at plug-in").

Pure functions. The measured power profile is used twice: where it actually
happened, and shifted to the plug-in time. Both are priced with the same
(spot) prices and CO₂ intensities, so only the timing differs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from efficharge.energy.series import SLOT, EnergySeries, floor_slot

#: A sample's reading is trusted for at most this long (e.g. if polls were missed).
MAX_SAMPLE_GAP = timedelta(minutes=5)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    watts: float

    @property
    def kwh(self) -> float:
        return self.watts * (self.end - self.start).total_seconds() / 3_600_000


@dataclass(frozen=True)
class CycleSavings:
    kwh: float
    cost_actual_eur: float | None
    cost_baseline_eur: float | None
    co2_actual_g: float | None
    co2_baseline_g: float | None
    #: energy-weighted green-power share in %, measured where the grid data is final
    ren_actual_pct: float | None = None
    ren_baseline_pct: float | None = None

    @property
    def saved_eur(self) -> float | None:
        if self.cost_actual_eur is None or self.cost_baseline_eur is None:
            return None
        return self.cost_baseline_eur - self.cost_actual_eur

    @property
    def avoided_co2_g(self) -> float | None:
        if self.co2_actual_g is None or self.co2_baseline_g is None:
            return None
        return self.co2_baseline_g - self.co2_actual_g


def intervals_from_samples(
    samples: Sequence[tuple[datetime, float]], end: datetime, max_gap: timedelta = MAX_SAMPLE_GAP
) -> list[Interval]:
    """Step function: each reading holds until the next one (at most ``max_gap``)."""
    ordered = sorted(samples)
    out: list[Interval] = []
    for i, (ts, watts) in enumerate(ordered):
        if ts >= end:
            break
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else end
        stop = min(nxt, ts + max_gap, end)
        if stop > ts and watts > 0:
            out.append(Interval(ts, stop, watts))
    return out


def flat_profile(start: datetime, duration: timedelta, kwh: float) -> list[Interval]:
    """For plugs without metering: the battery's energy spread evenly over the duration."""
    watts = kwh * 3_600_000 / duration.total_seconds()
    return [Interval(start, start + duration, watts)]


def per_slot(
    intervals: Sequence[Interval], shift: timedelta = timedelta(0)
) -> dict[datetime, float]:
    """kWh per 15-min slot, optionally with the whole profile moved by ``shift``."""
    out: dict[datetime, float] = {}
    for iv in intervals:
        start, end = iv.start + shift, iv.end + shift
        t = floor_slot(start)
        while t < end:
            overlap = (min(end, t + SLOT) - max(start, t)).total_seconds()
            if overlap > 0:
                out[t] = out.get(t, 0.0) + iv.watts * overlap / 3_600_000
            t += SLOT
    return out


def compute_savings(
    intervals: Sequence[Interval],
    *,
    actual_start: datetime,
    baseline_start: datetime,
    series: EnergySeries,
) -> CycleSavings:
    actual = per_slot(intervals)
    baseline = per_slot(intervals, shift=baseline_start - actual_start)

    def eur(profile: dict[datetime, float]) -> float | None:
        total = series.weighted_sum(series.price, profile)  # EUR/MWh × kWh
        return total / 1000 if total is not None else None

    def co2(profile: dict[datetime, float]) -> float | None:
        return series.weighted_sum(series.co2, profile) if series.co2 is not None else None

    def ren(profile: dict[datetime, float]) -> float | None:
        energy = sum(profile.values())
        total = series.weighted_sum(series.ren_share, profile)
        return total / energy if total is not None and energy > 0 else None

    return CycleSavings(
        kwh=sum(iv.kwh for iv in intervals),
        cost_actual_eur=eur(actual),
        cost_baseline_eur=eur(baseline),
        co2_actual_g=co2(actual),
        co2_baseline_g=co2(baseline),
        ren_actual_pct=ren(actual),
        ren_baseline_pct=ren(baseline),
    )
