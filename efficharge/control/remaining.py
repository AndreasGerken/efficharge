"""How long a running charge will still take. Pure functions.

Lithium chargers work in two phases (CC/CV):

1. *Plateau* (constant current): roughly constant power until the battery is ~80-90 %
   full. How long it lasts depends on how empty the battery was, which we don't know.
2. *Decline* (constant voltage): the power falls, roughly exponentially, until the
   charger switches off at a fixed power (e.g. ~20 W for a 180 W e-bike charger).

So:

* on the plateau: the planned duration (``planner.duration``, the same 90 % value the
  planner and the settings page use) minus the time charged so far: "up to …"
* in the decline: the falling power extrapolated to the switch-off power learned from
  past full charges (their last reading before switching off); without history 10 %
  of the plateau power: "about …"

The switch-off power is the same however empty the battery was, so every full charge
teaches it, also one that started almost full (which is all decline).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

Sample = tuple[datetime, float]  # (time, watts)

DECLINE_BELOW = 0.88  # a running charge is in the decline below this share of its power
CUTOFF_SHARE = 0.10  # without history: chargers switch off at about this share
STANDBY_BELOW_W = 2.0  # readings below this are the charger idling, not charging
FIT_WINDOW = timedelta(minutes=20)  # the extrapolation uses the recent readings


def charging_power(watts: Sequence[float]) -> float:
    """The plateau power: 90th percentile (robust against single spikes)."""
    ordered = sorted(watts)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]


@dataclass(frozen=True)
class CutoffModel:
    #: past switch-off powers (the last reading before the charger switched off), W
    cutoffs_w: tuple[float, ...]

    def cutoff_w(self) -> float:
        return statistics.median(self.cutoffs_w)


def learn(charges: Sequence[tuple[Sequence[Sample], datetime]]) -> CutoffModel | None:
    """From past full charges: (samples from start to end, end time)."""
    cutoffs = []
    for samples, end in charges:
        charging = [w for ts, w in sorted(samples) if ts <= end and w >= STANDBY_BELOW_W]
        if charging:
            cutoffs.append(charging[-1])
    return CutoffModel(tuple(cutoffs)) if cutoffs else None


@dataclass(frozen=True)
class Remaining:
    kind: str  # "plateau" (a 90 % value: "up to") | "decline" (extrapolated: "about")
    left: timedelta  # 0 = any moment now


def estimate(
    samples: Sequence[Sample],
    now: datetime,
    model: CutoffModel | None,
    *,
    started: datetime,
    planned: timedelta | None,
) -> Remaining | None:
    samples = sorted(s for s in samples if s[1] >= STANDBY_BELOW_W)
    watts = [w for _, w in samples]
    if len(watts) >= 3:
        power = charging_power(watts)
        current = sorted(watts[-3:])[1]  # median: smooth single odd readings
        if power > 0 and current / power < DECLINE_BELOW:
            cutoff = model.cutoff_w() if model else CUTOFF_SHARE * power
            # only readings of the decline itself (plateau readings would steepen the fit)
            declining = [(ts, w) for ts, w in samples if w < DECLINE_BELOW * power]
            left = _extrapolate(declining, now, cutoff)
            if left is not None:
                return Remaining("decline", left)
    if planned is not None:
        return Remaining("plateau", max(planned - (now - started), timedelta(0)))
    return None


def _extrapolate(samples: Sequence[Sample], now: datetime, cutoff_w: float) -> timedelta | None:
    """Time until the falling power reaches ``cutoff_w``: least-squares line through
    log(power) of the recent readings (exponential decay)."""
    pts = [((ts - now).total_seconds(), math.log(w)) for ts, w in samples if ts >= now - FIT_WINDOW]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx  # per second, < 0 when falling
    if slope >= 0:
        return None
    level_now = my - slope * mx  # fitted log(power) now (x = 0)
    return timedelta(seconds=max((level_now - math.log(cutoff_w)) / -slope, 0.0))
