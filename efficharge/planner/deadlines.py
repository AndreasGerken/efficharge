"""Deadline rules: "plugged in on Mon–Fri before 15:00 → ready by 17:00", or
"… → ready within 6 h". When no rule matches, the fallback applies: ready within
12 h (adjustable per device, see ``default``).

Rules are written in the user's local time. ``ready_by`` always means the *next*
occurrence of that local time after plug-in, so "plugged in after 15:00 → ready
by 09:30" automatically means 09:30 the next day.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

ALL_DAYS = frozenset(range(7))  # 0 = Monday
WORKDAYS = frozenset(range(5))
DEFAULT_DEADLINE = timedelta(hours=12)


@dataclass(frozen=True)
class DeadlineRule:
    ready_by: time | None = None  # either this …
    within: timedelta | None = None  # … or this
    weekdays: frozenset[int] = field(default=ALL_DAYS)
    plugged_from: time = time(0, 0)  # inclusive
    plugged_until: time = time(0, 0)  # exclusive; until <= from means "wraps past midnight"
    priority: int = 0

    def matches(self, local: datetime) -> bool:
        if local.weekday() not in self.weekdays:
            return False
        t = local.time().replace(tzinfo=None)
        if self.plugged_from == self.plugged_until:  # whole day
            return True
        if self.plugged_from < self.plugged_until:
            return self.plugged_from <= t < self.plugged_until
        return t >= self.plugged_from or t < self.plugged_until


def next_local_time(after: datetime, at: time, tz: ZoneInfo) -> datetime:
    """Next moment strictly after ``after`` whose local wall time is ``at`` (returned in UTC)."""
    local = after.astimezone(tz)
    for days in range(0, 3):
        day = (local + timedelta(days=days)).date()
        candidate = datetime.combine(day, at, tzinfo=tz)
        # Round-trip through UTC normalises wall times that don't exist (DST gap).
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc > after:
            return candidate_utc
    raise AssertionError("unreachable")


def resolve_deadline(
    plugged_at: datetime,
    rules: Sequence[DeadlineRule],
    tz: ZoneInfo,
    *,
    override: datetime | None = None,
    default: timedelta = DEFAULT_DEADLINE,
) -> datetime:
    """Deadline in UTC. Precedence: one-off override > first matching rule > default."""
    if override is not None:
        return override.astimezone(UTC)
    index = matching_rule(plugged_at, rules, tz)
    if index is not None:
        rule = rules[index]
        if rule.within is not None:
            return (plugged_at + rule.within).astimezone(UTC)
        return next_local_time(plugged_at, rule.ready_by, tz)
    return (plugged_at + default).astimezone(UTC)


def matching_rule(plugged_at: datetime, rules: Sequence[DeadlineRule], tz: ZoneInfo) -> int | None:
    """Index (into ``rules``) of the rule that decides the deadline, or None."""
    local = plugged_at.astimezone(tz)
    # sorted() is stable: equal priorities keep the user's order.
    for index in sorted(range(len(rules)), key=lambda i: -rules[i].priority):
        if rules[index].matches(local):
            return index
    return None
