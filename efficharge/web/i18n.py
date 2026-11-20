"""Translations and locale-aware formatting.

Source language is English (the msgids). Catalogs are ``.po`` files in
``web/locales/<lang>/LC_MESSAGES/messages.po``, loaded at start-up (no compiled
``.mo`` files in the repo). Adding a language = adding a ``.po`` file + an entry in
``SUPPORTED``. ``tests/test_i18n.py`` fails if a German translation is missing.
"""

from __future__ import annotations

import gettext as _gettext
import io
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from babel import Locale
from babel.dates import format_datetime as _format_datetime
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.numbers import format_currency, format_decimal, format_percent

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED = ("en", "de")
DEFAULT = "de"  # for Germany: German unless the browser asks for English

_current: ContextVar[str] = ContextVar("locale", default=DEFAULT)
_catalogs: dict[str, _gettext.NullTranslations] = {}


def load_catalogs() -> None:
    _catalogs.clear()
    for lang in SUPPORTED:
        po = LOCALES_DIR / lang / "LC_MESSAGES" / "messages.po"
        if not po.exists():
            _catalogs[lang] = _gettext.NullTranslations()
            continue
        with po.open("rb") as f:
            catalog = read_po(f, locale=lang)
        buf = io.BytesIO()
        write_mo(buf, catalog, use_fuzzy=False)
        buf.seek(0)
        _catalogs[lang] = _gettext.GNUTranslations(buf)


def set_locale(lang: str) -> None:
    _current.set(lang if lang in SUPPORTED else DEFAULT)


def get_locale() -> str:
    return _current.get()


def _t() -> _gettext.NullTranslations:
    if not _catalogs:
        load_catalogs()
    return _catalogs.get(get_locale(), _gettext.NullTranslations())


def gettext(message: str, **params: object) -> str:
    text = _t().gettext(message)
    return text % params if params else text


def ngettext(singular: str, plural: str, n: int, **params: object) -> str:
    text = _t().ngettext(singular, plural, n)
    return text % {"num": n, **params}


_ = gettext


def negotiate(accept_language: str | None) -> str:
    """Pick a supported locale from an Accept-Language header."""
    if not accept_language:
        return DEFAULT
    ranked = []
    for part in accept_language.split(","):
        lang, _sep, q = part.strip().partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        ranked.append((weight, lang.split("-")[0].lower()))
    for _w, lang in sorted(ranked, reverse=True):
        if lang in SUPPORTED:
            return lang
    return DEFAULT


# ── formatting (always in the current locale) ──


def fmt_time(dt: datetime | None, tz: ZoneInfo) -> str:
    return _format_datetime(dt, "HH:mm", tzinfo=tz, locale=get_locale()) if dt else "–"


def fmt_day_time(dt: datetime | None, tz: ZoneInfo, now: datetime | None = None) -> str:
    """ "14:30" today, "Fri 07:30" otherwise."""
    if dt is None:
        return "–"
    if now is not None and dt.astimezone(tz).date() == now.astimezone(tz).date():
        return fmt_time(dt, tz)
    return _format_datetime(dt, "EEE HH:mm", tzinfo=tz, locale=get_locale())


def fmt_duration(d: timedelta) -> str:
    minutes = round(d.total_seconds() / 60)
    h, m = divmod(minutes, 60)
    if h and m:
        return gettext("%(h)d h %(m)d min", h=h, m=m)
    if h:
        return gettext("%(h)d h", h=h)
    return gettext("%(m)d min", m=m)


def fmt_eur(value: float | None) -> str:
    if value is None:
        return "–"
    return format_currency(value, "EUR", locale=get_locale())


def fmt_ct(value_eur: float | None) -> str:
    """Small amounts read better in cents: "4,2 ct"."""
    if value_eur is None:
        return "–"
    return f"{format_decimal(value_eur * 100, '0.0', locale=get_locale())} ct"


def fmt_num(value: float | None, pattern: str = "#,##0.##") -> str:
    return format_decimal(value, pattern, locale=get_locale()) if value is not None else "–"


def fmt_pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "–"
    # "-28%" instead of "-28 %": shorter, reads as one value in the small labels
    text = format_percent(value / 100, "#,##0%", locale=get_locale()).replace("\xa0", "")
    return ("+" + text) if signed and value > 0 else text


def weekday_names() -> list[str]:
    """Short names Monday..Sunday in the current locale."""
    names = Locale.parse(get_locale()).days["format"]["abbreviated"]
    return [names[i] for i in range(7)]


def weekdays_text(mask: int) -> str:
    """Bitmask (bit 0 = Monday) → "Mo–Fr", "Mo, Mi, Fr", "Mo–Mi, Sa, So" (current locale).

    Three or more days in a row become a range. Without the abbreviation dot, so it
    reads like the time ranges next to it ("Mo–Fr, 08:00–15:00")."""
    names = [n.rstrip(".") for n in weekday_names()]
    days = [d for d in range(7) if mask >> d & 1]
    parts, i = [], 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and days[j + 1] == days[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{names[days[i]]}–{names[days[j]]}")
        else:
            parts.extend(names[d] for d in days[i : j + 1])
        i = j + 1
    return ", ".join(parts)
