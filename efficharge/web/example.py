"""The example charge on the start page: one real demo charge, drawn by ``charts.js``.

``example_charge.json`` holds the data of one finished charge of the demo (simulated
battery, real DE-LU prices and green-power shares from Energy-Charts). ``payload()`` turns
it into the same shape as a plug's ``chart.json`` (``web.data.chart_payload``), so the
start page shows the dashboard's charts with the visitor's language, and ``cycle()`` gives
the charge itself for the usual labels (``explain.cycle_chips``). To replace the example,
write a new file with the same keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from efficharge.db.models import Cycle
from efficharge.energy.series import SLOT
from efficharge.web.i18n import _, fmt_day_time, get_locale

DATA = Path(__file__).parent / "example_charge.json"
_TIMES = ("plugged_at", "actual_start", "end_at", "deadline")


@cache
def _data() -> dict:
    return json.loads(DATA.read_text())


def tz() -> ZoneInfo:
    return ZoneInfo(_data()["timezone"])


def cycle() -> Cycle:
    """The example charge as a (never stored) ``Cycle``."""
    fields = dict(_data()["cycle"])
    for key in _TIMES:
        fields[key] = datetime.fromisoformat(fields[key])
    return Cycle(**fields)


def now() -> datetime:
    """The example is seen just after the charge ended."""
    return cycle().end_at + timedelta(minutes=5)


def _ms(value: str | datetime) -> int:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return int(dt.timestamp() * 1000)


def _steps(points: list[list], scale: float = 1.0) -> list[list[float]]:
    pts = [[_ms(t), round(v * scale, 2)] for t, v in points]
    if pts:  # close the last step
        pts.append([pts[-1][0] + int(SLOT.total_seconds() * 1000), pts[-1][1]])
    return pts


def payload() -> dict:
    data, c, zone = _data(), cycle(), tz()
    duration = timedelta(seconds=c.plan["duration_s"])

    def band(kind: str, frm: datetime, to: datetime, label: str) -> dict:
        return {"from": _ms(frm), "to": _ms(to), "label": label, "kind": kind, "live": []}

    when = fmt_day_time(c.plugged_at, zone, now())
    ready = f"{_('ready by')} {c.deadline.astimezone(zone):%H:%M}"
    return {
        "locale": get_locale(),
        "timezone": str(zone),
        "now": _ms(now()),
        "range": [_ms(t) for t in data["range"]],
        "range_live": [False, False],
        "bands": [
            band("baseline", c.plugged_at, c.plugged_at + duration, _("if charged right away")),
            band("charging", c.actual_start, c.end_at, _("charging")),
        ],
        "lines": [
            {"x": _ms(c.plugged_at), "label": _("plugged in %(t)s", t=when), "kind": "plugin"},
            {"x": _ms(c.deadline), "label": ready, "kind": "deadline"},
        ],
        "charts": [
            {
                "id": "green",
                "title": _("Green power share"),
                "unit": "%",
                "stepped": True,
                "points": _steps(data["green_pct"]),
                "min": 0,
                "max": 100,
            },
            {
                "id": "price",
                "title": _("Electricity price"),
                "unit": "ct/kWh",
                "stepped": True,
                "points": _steps(data["price_eur_mwh"], 0.1),
            },
            {
                "id": "power",
                "title": _("Charger power"),
                "unit": "W",
                "stepped": True,
                "points": [[_ms(t), w] for t, w in data["power_w"]],
                "min": 0,
            },
        ],
    }
