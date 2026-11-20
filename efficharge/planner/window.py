"""Find the best contiguous charging window before a deadline.

Pure functions: no I/O, no clock. ``now`` is always passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from efficharge.energy.series import EnergySeries, ceil_slot


class Priority(StrEnum):
    ECO = "eco"
    BALANCED = "balanced"
    MONEY = "money"


@dataclass(frozen=True)
class Weights:
    """How much price and renewable share count. Normalised to sum 1."""

    price: float
    eco: float

    def __post_init__(self) -> None:
        if self.price < 0 or self.eco < 0 or self.price + self.eco == 0:
            raise ValueError("weights must be >= 0 and not both 0")

    @classmethod
    def from_priority(cls, priority: Priority) -> Weights:
        return PRESETS[priority]

    @classmethod
    def from_slider(cls, price_weight: float) -> Weights:
        """``price_weight`` in [0, 1]: 0 = only green power, 1 = only price."""
        price_weight = min(1.0, max(0.0, price_weight))
        return cls(price=price_weight, eco=1.0 - price_weight)


# "X first": X dominates, the other goal still breaks near-ties.
PRESETS: dict[Priority, Weights] = {
    Priority.ECO: Weights(price=0.2, eco=0.8),
    Priority.BALANCED: Weights(price=0.5, eco=0.5),
    Priority.MONEY: Weights(price=0.8, eco=0.2),
}


class PlanReason(StrEnum):
    """Machine-readable reason. The web layer translates it, the core never builds text."""

    OPTIMAL = "optimal"
    NOW_IS_BEST = "now_is_best"
    DEADLINE_TOO_CLOSE = "deadline_too_close"  # no full window fits → start now
    NO_DATA = "no_data"  # nothing known for the horizon → start now
    PARTIAL_DATA = "partial_data"  # deadline beyond published prices; best known window
    #: Set by the state machine, not the planner: the best window starts within a few
    #: minutes, so we start right away instead of switching off briefly.
    #: ``Plan.chosen`` keeps the window's real start for the explanation.
    WINDOW_STARTS_SOON = "window_starts_soon"


@dataclass(frozen=True)
class WindowStats:
    start: datetime
    end: datetime
    avg_price: float | None  # EUR/MWh
    avg_ren_share: float | None  # %


@dataclass(frozen=True)
class Plan:
    start: datetime
    end: datetime
    deadline: datetime
    duration: timedelta
    reason: PlanReason
    chosen: WindowStats
    baseline: WindowStats  # "start right now", for savings and the ghost band in the chart
    eco_choice: WindowStats | None = None  # what "Eco first" would pick
    money_choice: WindowStats | None = None  # what "Money first" would pick
    #: the green-share forecast didn't cover the whole horizon, so the price alone decided
    green_ignored: bool = False

    @property
    def priorities_agree(self) -> bool:
        """Eco first and Money first pick (nearly) the same window: ≥ 75 % overlap."""
        if self.eco_choice is None or self.money_choice is None:
            return False
        e, m = self.eco_choice, self.money_choice
        overlap = min(e.end, m.end) - max(e.start, m.start)
        return overlap >= 0.75 * self.duration


def _stats(series: EnergySeries, start: datetime, duration: timedelta) -> WindowStats:
    end = start + duration
    return WindowStats(
        start=start,
        end=end,
        avg_price=series.average(series.price, start, end),
        avg_ren_share=series.average(series.ren_share, start, end),
    )


def candidate_starts(now: datetime, latest_start: datetime) -> list[datetime]:
    """``now`` plus every 15-min grid point up to ``latest_start``."""
    starts = [now]
    t = ceil_slot(now)
    if t == now:
        t = ceil_slot(now + timedelta(microseconds=1))
    while t <= latest_start:
        starts.append(t)
        t += timedelta(minutes=15)
    return starts


def _normalise(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _best(candidates: list[WindowStats], weights: Weights) -> WindowStats:
    total = weights.price + weights.eco
    wp, we = weights.price / total, weights.eco / total
    prices = _normalise([c.avg_price or 0.0 for c in candidates]) if wp else None
    # Higher renewable share is better, so invert it (100 - share).
    dirt = _normalise([100.0 - (c.avg_ren_share or 0.0) for c in candidates]) if we else None
    best_i, best_score = 0, float("inf")
    for i in range(len(candidates)):
        score = (wp * prices[i] if prices else 0.0) + (we * dirt[i] if dirt else 0.0)
        # Strictly smaller: on a tie the earliest window wins (charged sooner = safer).
        if score < best_score - 1e-12:
            best_i, best_score = i, score
    return candidates[best_i]


def best_window(
    series: EnergySeries,
    *,
    now: datetime,
    deadline: datetime,
    duration: timedelta,
    weights: Weights,
) -> Plan:
    """Choose when to start charging.

    Fallbacks always start now, because "charged inefficiently" beats "not charged".
    """
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    baseline = _stats(series, now, duration)

    def start_now(reason: PlanReason) -> Plan:
        return Plan(now, now + duration, deadline, duration, reason, baseline, baseline)

    if deadline - now < duration:
        return start_now(PlanReason.DEADLINE_TOO_CLOSE)

    # Windows must lie inside the known data. The green-share forecast often ends earlier
    # than the prices (it reaches midnight, prices for tomorrow come at ~13:00):
    # - green counts most ("Eco first"): only the hours with a green forecast are
    #   searched. When the forecast grows, the plan is redone (energy.store.upsert).
    # - otherwise, or if that span is too short for a charge: an incomplete forecast is
    #   not used at all, the price alone decides (comparing windows with and without a
    #   green value would be unfair either way).
    price_matters = weights.price > 0
    eco_first = weights.eco > weights.price

    def horizon(need_ren: bool) -> tuple[datetime, datetime]:
        known = series.known_until(need_price=price_matters, need_ren=need_ren, after=now)
        return known, min(deadline, known) - duration

    known_until, latest_start = horizon(need_ren=eco_first)
    if latest_start < now and eco_first and price_matters:
        known_until, latest_start = horizon(need_ren=False)
    if latest_start < now:
        return start_now(PlanReason.NO_DATA)
    horizon_end = latest_start + duration

    green_complete = all(t in series.ren_share for t in series.slots(now, horizon_end))
    green_ignored = price_matters and weights.eco > 0 and not green_complete
    if green_ignored:
        weights = Weights(price=1.0, eco=0.0)
    starts = candidate_starts(now, latest_start)
    scored = [_stats(series, s, duration) for s in starts]
    best = _best(scored, weights)
    chosen = best
    if chosen.start == now:
        reason = PlanReason.NOW_IS_BEST
    elif known_until < deadline:
        reason = PlanReason.PARTIAL_DATA
    else:
        reason = PlanReason.OPTIMAL

    plan = Plan(
        chosen.start,
        chosen.end,
        deadline,
        duration,
        reason,
        chosen,
        baseline,
        green_ignored=green_ignored,
    )
    # "what would the other priority pick": needs the green share for the whole horizon
    if green_complete:
        eco = _best(scored, PRESETS[Priority.ECO])
        money = _best(scored, PRESETS[Priority.MONEY])
        plan = replace(plan, eco_choice=eco, money_choice=money)
    return plan
