"""Estimate how long the next charge will take.

The user's own estimate is a *virtual charge* that is always part of the statistics
(taken as is: "3 h" means 3 h). Real charges join it, and the planner uses a high percentile (90 %)
of all of them, so the battery is ready by the deadline in about 9 of 10 cases.

This gives a smooth transition from guess to measurement without any switch-over:

* no history: only the virtual charge counts (the estimate)
* a few real charges: the percentile still lies close to the virtual charge
* ~10+ real charges: the percentile is decided by real charges; the virtual one
  is just one sample among many
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta

PRIOR_BUFFER = 1.0  # the user's estimate as is (was +50 %; users know their charger)
PERCENTILE = 0.9  # ready by the deadline in ~9 of 10 cases
LAST_N = 20  # only recent charges count (batteries age, habits change)


def prior_weight(real_charges: int) -> int:
    """How many times the user's estimate counts: 3× at first, so a single odd charge
    barely moves the plan, then less, so that after ~10 charges the real ones decide
    (a 90 % value can't drop below a value that makes up more than 10 % of all)."""
    return max(1, 3 - real_charges // 3)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (like numpy's default), ``q`` in [0, 1]."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def estimate_duration(
    past_durations: Sequence[timedelta],
    user_estimate: timedelta,
    *,
    prior_buffer: float = PRIOR_BUFFER,
    q: float = PERCENTILE,
    last_n: int = LAST_N,
) -> timedelta:
    """Planned charge duration.

    ``past_durations``: oldest → newest, only charges that ended because the battery
    was full (not unplugged, not paused).
    """
    virtual = user_estimate.total_seconds() * prior_buffer
    real = [d.total_seconds() for d in past_durations[-last_n:] if d > timedelta(0)]
    seconds = percentile([virtual] * prior_weight(len(real)) + real, q)
    return timedelta(minutes=math.ceil(seconds / 60))
