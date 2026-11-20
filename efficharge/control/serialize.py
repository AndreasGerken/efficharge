"""DeviceState / Plan <-> JSON-safe dicts for the DB and the web layer."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from efficharge.control.machine import DeviceState, Phase
from efficharge.planner.window import Plan, PlanReason, WindowStats


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def _pdt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v is not None else None


def window_to_dict(w: WindowStats | None) -> dict[str, Any] | None:
    if w is None:
        return None
    return {
        "start": _dt(w.start),
        "end": _dt(w.end),
        "avg_price": w.avg_price,
        "avg_ren_share": w.avg_ren_share,
    }


def window_from_dict(d: dict[str, Any] | None) -> WindowStats | None:
    if d is None:
        return None
    return WindowStats(_pdt(d["start"]), _pdt(d["end"]), d["avg_price"], d["avg_ren_share"])


def plan_to_dict(p: Plan | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "start": _dt(p.start),
        "end": _dt(p.end),
        "deadline": _dt(p.deadline),
        "duration_s": p.duration.total_seconds(),
        "reason": p.reason.value,
        "chosen": window_to_dict(p.chosen),
        "baseline": window_to_dict(p.baseline),
        "eco_choice": window_to_dict(p.eco_choice),
        "money_choice": window_to_dict(p.money_choice),
        "priorities_agree": p.priorities_agree,
        "green_ignored": p.green_ignored,
    }


def plan_from_dict(d: dict[str, Any] | None) -> Plan | None:
    if d is None:
        return None
    return Plan(
        start=_pdt(d["start"]),
        end=_pdt(d["end"]),
        deadline=_pdt(d["deadline"]),
        duration=timedelta(seconds=d["duration_s"]),
        reason=PlanReason(d["reason"]),
        chosen=window_from_dict(d["chosen"]),
        baseline=window_from_dict(d["baseline"]),
        eco_choice=window_from_dict(d.get("eco_choice")),
        money_choice=window_from_dict(d.get("money_choice")),
        green_ignored=d.get("green_ignored", False),
    )


_DT_FIELDS = (
    "since",
    "expected_until",
    "plugged_at",
    "override_deadline",
    "low_since",
    "charging_since",
    "drop_since",
    "last_at",
)


def state_to_dict(s: DeviceState) -> dict[str, Any]:
    d: dict[str, Any] = {
        "phase": s.phase.value,
        "switch_on": s.switch_on,
        "expected_switch": s.expected_switch,
        "plan": plan_to_dict(s.plan),
        "above_count": s.above_count,
        "saw_load": s.saw_load,
        "last_watts": s.last_watts,
        "peak_watts": s.peak_watts,
        "low_after_w": s.low_after_w,
    }
    for name in _DT_FIELDS:
        d[name] = _dt(getattr(s, name))
    return d


def state_from_dict(d: dict[str, Any] | None) -> DeviceState:
    if not d:
        return DeviceState()
    return DeviceState(
        phase=Phase(d["phase"]),
        switch_on=d.get("switch_on"),
        expected_switch=d.get("expected_switch"),
        plan=plan_from_dict(d.get("plan")),
        above_count=d.get("above_count", 0),
        saw_load=d.get("saw_load", False),
        last_watts=d.get("last_watts"),
        peak_watts=d.get("peak_watts", 0.0),
        low_after_w=d.get("low_after_w"),
        **{name: _pdt(d.get(name)) for name in _DT_FIELDS},
    )
