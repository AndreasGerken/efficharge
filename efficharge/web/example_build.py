"""Build ``example_charge.json`` (the start page's example) from a real past day.

    efficharge make-example 2026-04-17 [--plugged 09:00] [--ready 18:00]

Real data of that day from Energy-Charts (exchange price, measured green-power share, CO₂),
the app's own planner (balanced priority) and savings accounting, and the demo's
simulated battery and charger. Nothing is made up but the battery.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from efficharge.control.savings import compute_savings, intervals_from_samples
from efficharge.control.serialize import plan_to_dict
from efficharge.devices.simulated import SimBattery, SimDevice
from efficharge.energy.client import EnergyChartsClient
from efficharge.energy.series import SLOT, EnergySeries
from efficharge.planner.window import PRESETS, Priority, best_window
from efficharge.web.example import DATA

TZ = "Europe/Berlin"
STEP = timedelta(minutes=1)
#: how long the charger runs at the plug-in before efficharge notices it and switches off
DETECTION = timedelta(minutes=1)
MARGIN = timedelta(hours=2)  # chart range around plug-in and deadline


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def simulate(plugged: datetime, start: datetime, until: datetime) -> tuple[list, datetime]:
    """The demo battery: plugged in (draws until detected), off, on at ``start`` until full.
    Returns the power reading of every minute and when the battery was full."""
    dev = SimDevice("example", "example", on=True, battery=SimBattery())
    readings: list[tuple[datetime, float]] = []
    full = None
    t = plugged - MARGIN
    dev.battery, dev.updated = None, t
    while t <= until:
        if t == plugged:
            dev.battery = SimBattery()
        dev.on = not (plugged + DETECTION <= t < start)
        dev.advance(t)
        watts = round(dev.draw(), 1)
        if full is None and t > start and dev.battery is not None and dev.battery.soc >= 1.0:
            full = t
        readings.append((t, watts))
        t += STEP
    if full is None:
        raise SystemExit("the battery isn't full before the end of the chart: later deadline?")
    return readings, full


async def build(day: date, plugged_at: time, ready_by: time) -> dict:
    tz = ZoneInfo(TZ)
    plugged = datetime.combine(day, plugged_at, tz)
    deadline = datetime.combine(day, ready_by, tz)
    start, end = plugged - MARGIN, deadline + MARGIN
    client = EnergyChartsClient()
    try:
        raw = await client.fetch_past(start - SLOT, end + SLOT)
    finally:
        await client.aclose()
    series = EnergySeries.from_points(raw.price, raw.ren_share, raw.co2)
    # the demo battery's charge time, as the planner would have learned it
    _, full_after = simulate(plugged, plugged, end + timedelta(hours=6))
    duration = timedelta(minutes=round((full_after - plugged).total_seconds() / 60))
    plan = best_window(
        series,
        now=plugged,
        deadline=deadline,
        duration=duration,
        weights=PRESETS[Priority.BALANCED],
    )
    readings, full = simulate(plugged, plan.start, end)
    charge = [(t, w) for t, w in readings if plan.start <= t < full]
    savings = compute_savings(
        intervals_from_samples(charge, full),
        actual_start=plan.start,
        baseline_start=plugged,
        series=series,
    )

    def points(values: dict) -> list:
        return [
            [_iso(t), round(v, 2)]
            for t, v in sorted(values.items())
            if v is not None and start - SLOT <= t < end
        ]

    # the chart only needs the changes (it draws steps); keep the last reading as the end
    changes = [r for i, r in enumerate(readings) if i == 0 or r[1] != readings[i - 1][1]]
    changes.append(readings[-1])
    return {
        "about": f"The start page's example, made with `efficharge make-example {day}`: real "
        "DE-LU prices, measured green-power shares and CO₂ from Energy-Charts, the app's "
        "planner (balanced) and the demo's simulated battery. See web/example_build.py.",
        "timezone": TZ,
        "range": [_iso(start), _iso(end)],
        "cycle": {
            "plugged_at": _iso(plugged),
            "actual_start": _iso(plan.start),
            "end_at": _iso(full),
            "deadline": _iso(deadline),
            "end_reason": "full",
            "plan": plan_to_dict(plan),
            "kwh": round(savings.kwh, 6),
            "cost_actual_eur": savings.cost_actual_eur,
            "cost_baseline_eur": savings.cost_baseline_eur,
            "co2_actual_g": savings.co2_actual_g,
            "co2_baseline_g": savings.co2_baseline_g,
            "ren_actual_pct": savings.ren_actual_pct,
            "ren_baseline_pct": savings.ren_baseline_pct,
        },
        "price_eur_mwh": points(raw.price),
        "green_pct": points(raw.ren_share),
        "power_w": [[_iso(t), w] for t, w in changes],
    }


async def main(day: str, plugged: str, ready: str, out: Path = DATA) -> None:
    when = date.fromisoformat(day)
    data = await build(when, time.fromisoformat(plugged), time.fromisoformat(ready))
    out.write_text(json.dumps(data, indent=1) + "\n")
    c = data["cycle"]
    tz = ZoneInfo(TZ)

    def local(key: str) -> str:
        return f"{datetime.fromisoformat(c[key]).astimezone(tz):%H:%M}"

    print(
        f"{out}: charged {local('actual_start')}–{local('end_at')}, "
        f"cost {c['cost_actual_eur'] * 100:.1f} ct "
        f"instead of {c['cost_baseline_eur'] * 100:.1f} ct, "
        f"green {c['ren_actual_pct']:.0f} % instead of {c['ren_baseline_pct']:.0f} %, "
        f"{c['co2_baseline_g'] - c['co2_actual_g']:.0f} g CO₂ avoided"
    )
