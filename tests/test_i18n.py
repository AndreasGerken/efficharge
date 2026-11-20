"""Every user-facing string must be translated, with the same placeholders."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import read_po

from efficharge.web import i18n

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "efficharge" / "web"
PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")


def source_msgids() -> set[str | tuple[str, ...]]:
    method_map = [("**.py", "python"), ("templates/**.html", "jinja2")]
    options = {"templates/**.html": {"extensions": "jinja2.ext.i18n"}}
    keywords = {"_": None, "gettext": None, "ngettext": (1, 2)}
    ids: set[str | tuple[str, ...]] = set()
    for _file, _line, message, _comments, _ctx in extract_from_dir(
        str(WEB), method_map, options, keywords=keywords
    ):
        ids.add(message if isinstance(message, str) else tuple(message))
    return ids


def catalog(lang: str):
    with (WEB / "locales" / lang / "LC_MESSAGES" / "messages.po").open("rb") as f:
        return read_po(f, locale=lang)


@pytest.mark.parametrize("lang", [lang for lang in i18n.SUPPORTED if lang != "en"])
def test_all_strings_translated(lang):
    cat = catalog(lang)
    translated = {
        (m.id if isinstance(m.id, str) else tuple(m.id)): m
        for m in cat
        if m.id and m.string and not m.fuzzy and (isinstance(m.string, str) or all(m.string))
    }
    ids = source_msgids()
    assert len(ids) > 100  # the extraction itself works
    missing = sorted(map(str, ids - translated.keys()))
    assert not missing, f"untranslated in {lang}: {missing}"


@pytest.mark.parametrize("lang", [lang for lang in i18n.SUPPORTED if lang != "en"])
def test_placeholders_match(lang):
    for m in catalog(lang):
        if not m.id:
            continue
        ids = [m.id] if isinstance(m.id, str) else list(m.id)
        strings = [m.string] if isinstance(m.string, str) else list(m.string)
        expected = set(PLACEHOLDER.findall(" ".join(ids)))
        for s in strings:
            assert set(PLACEHOLDER.findall(s)) == expected, f"{m.id!r} -> {s!r}"
            assert s.count("%%") == " ".join(ids).count("%%") // len(ids), f"%% in {s!r}"


def test_runtime_translation_and_formats():
    i18n.load_catalogs()
    i18n.set_locale("de")
    assert i18n._("Charge now") == "Jetzt laden"
    assert i18n.fmt_eur(1.5) == "1,50\xa0€"
    assert i18n.ngettext("%(num)d charge", "%(num)d charges", 2) == "2 charges"  # not in catalog
    i18n.set_locale("en")
    assert i18n._("Charge now") == "Charge now"
    assert i18n.fmt_eur(1.5) == "€1.50"
    i18n.set_locale("xx")
    assert i18n.get_locale() == "de"  # unknown: the default


def test_negotiate():
    assert i18n.negotiate("de-DE,de;q=0.9,en;q=0.8") == "de"
    assert i18n.negotiate("fr-FR,en;q=0.5") == "en"
    assert i18n.negotiate("fr") == "de"  # neither: German, the site is for Germany
    assert i18n.negotiate(None) == "de"


def test_weekdays_text_uses_ranges():
    i18n.set_locale("de")
    assert i18n.weekdays_text(0b0011111) == "Mo–Fr"
    assert i18n.weekdays_text(0b1100000) == "Sa, So"
    assert i18n.weekdays_text(0b1010101) == "Mo, Mi, Fr, So"
    assert i18n.weekdays_text(0b1100111) == "Mo–Mi, Sa, So"
    i18n.set_locale("en")
    assert i18n.weekdays_text(0b0011111) == "Mon–Fri"


@pytest.mark.parametrize("lang", [lang for lang in i18n.SUPPORTED if lang != "en"])
def test_no_unused_translations(lang):
    """Texts removed from the code must leave the catalog too (no dead translations)."""
    ids = source_msgids()
    assert len(ids) > 100
    unused = [
        m.id
        for m in catalog(lang)
        if m.id and (m.id if isinstance(m.id, str) else tuple(m.id)) not in ids
    ]
    assert not unused, unused
