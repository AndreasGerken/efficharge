"""The hosted instance before launch: no demo, no registration, accounts made over SSH."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from efficharge.config import Config
from efficharge.crypto import generate_key
from efficharge.db.migrate import upgrade
from efficharge.users_cli import add_user, set_password
from efficharge.web.app import create_app
from efficharge.web.auth import reset_limiters


@pytest.fixture
def private(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        registration_open=False,
        demo=False,
    )
    reset_limiters()
    app = create_app(cfg, start_workers=False)
    with TestClient(app, headers={"accept-language": "en"}, follow_redirects=False) as c:
        yield c, url


def test_nothing_is_reachable_without_login(private):
    c, _ = private
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/demo").status_code == 404
    assert c.get("/register").status_code in (403, 404)
    for path in ("/devices", "/stats", "/accounts", "/account"):
        assert c.get(path).status_code in (303, 401), path
    login = c.get("/login")
    assert login.status_code == 200
    assert "/register" not in login.text and "/demo" not in login.text
    assert login.headers["x-robots-tag"] == "noindex, nofollow"
    assert "strict-transport-security" not in login.headers  # plain http here
    assert c.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"


def test_user_created_over_ssh_can_log_in(private):
    c, url = private
    asyncio.run(
        add_user(url, "Me@Example.org", "a-long-password", timezone="Europe/Berlin", locale="de")
    )
    with pytest.raises(SystemExit):
        asyncio.run(
            add_user(url, "me@example.org", "another-one!", timezone="Europe/Berlin", locale="de")
        )
    asyncio.run(set_password(url, "me@example.org", "the-new-password"))

    def login(password):
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)
        data = {"email": "me@example.org", "password": password}
        if token:
            data["csrf_token"] = token.group(1)
        headers = {}
        m = re.search(r'"X-CSRF-Token": "([^"]+)"', c.get("/login").text)
        if m:
            headers["X-CSRF-Token"] = m.group(1)
        return c.post("/login", data=data, headers=headers)

    assert login("a-long-password").status_code != 303
    r = login("the-new-password")
    assert r.status_code == 303 and r.headers["location"] == "/devices"
    # logged in: no plug yet, so the dashboard leads to the welcome in the settings
    assert c.get("/devices").headers["location"] == "/settings"
    assert "Welcome to efficharge!" in c.get("/settings?lang=en").text

    # a new password (e.g. after a stolen phone) logs out every existing session
    asyncio.run(set_password(url, "me@example.org", "yet-another-password"))
    assert c.get("/devices").headers["location"].startswith("/login")


def test_no_api_docs_and_no_server_details(private):
    c, _ = private
    assert c.get("/api/docs").status_code == 404
    assert c.get("/api/openapi.json").status_code == 404


def test_imprint_is_public_and_linked_when_configured(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'i.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        demo=False,
        imprint_name="Erika Muster",
        imprint_address="Musterstraße 1, 12345 Berlin",
        imprint_email="kontakt@example.org",
    )
    app = create_app(cfg, start_workers=False)
    with TestClient(app, headers={"accept-language": "de"}) as c:
        assert '<a href="/impressum">Impressum</a>' in c.get("/login").text
        page = c.get("/impressum")
        assert page.status_code == 200
        assert "Angaben gemäß § 5 DDG" in page.text
        assert "Musterstraße 1, 12345 Berlin" in page.text
        assert "mailto:kontakt@example.org" in page.text


def test_imprint_missing_config_means_no_page(private):
    c, _ = private
    assert c.get("/impressum").status_code == 404
    assert "/impressum" not in c.get("/login").text


def test_https_instance_sends_hsts(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        demo=False,
    )
    with TestClient(create_app(cfg, start_workers=False)) as c:
        assert c.get("/login").headers["strict-transport-security"] == "max-age=31536000"


def test_privacy_notice_is_public_in_both_languages(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        demo=False,
        imprint_name="Erika Muster",
        imprint_address="Musterstraße 1, 12345 Berlin",
        imprint_email="kontakt@example.org",
        hosting_provider="Example Hosting GmbH, Musterweg 2, 12345 Musterstadt",
        backup_days=21,
    )
    with TestClient(create_app(cfg, start_workers=False), headers={"accept-language": "de"}) as c:
        assert '<a href="/privacy">Datenschutz</a>' in c.get("/login").text
        de = c.get("/privacy")
        assert de.status_code == 200
        assert "Datenschutzerklärung" in de.text
        assert 'id="meross"' in de.text and "iotx-eu.meross.com" in de.text
        assert "mailto:kontakt@example.org" in de.text
        assert "Den Server betreibt Example Hosting GmbH" in de.text
        assert "höchstens\n    21 Tage" in de.text
        en = c.get("/privacy?lang=en")
        assert "Privacy notice" in en.text and 'id="meross"' in en.text
        assert c.get("/datenschutz", follow_redirects=False).headers["location"] == (
            "/privacy?lang=de"
        )


def test_privacy_notice_without_imprint_names_the_operator(private):
    c, _ = private
    page = c.get("/privacy")
    assert page.status_code == 200
    assert "operator of this Efficharge installation" in page.text
    assert "Hosting:" not in page.text  # no provider configured
    assert "at most\n    30 days" in page.text


def test_source_and_coffee_links(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
    upgrade(url)
    base = dict(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        demo=False,
    )
    with TestClient(create_app(Config(**base), start_workers=False)) as c:
        page = c.get("/login").text
        assert 'href="https://github.com/AndreasGerken/efficharge"' in page  # AGPL § 13
        assert "coffee" not in page.lower()  # no link unless configured
    cfg = Config(**base, coffee_url="https://buymeacoffee.com/someone", source_url="")
    with TestClient(create_app(cfg, start_workers=False)) as c:
        page = c.get("/login").text
        assert 'href="https://buymeacoffee.com/someone"' in page
        assert "github.com" not in page


def test_indexing_can_be_allowed_at_launch(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        allow_indexing=True,
    )
    with TestClient(create_app(cfg, start_workers=False)) as c:
        assert "x-robots-tag" not in c.get("/login").headers
        robots = c.get("/robots.txt").text
        assert "Allow: /$" in robots and "Allow: /privacy" in robots and "Disallow: /\n" in robots


def test_about_is_public_on_a_private_instance(private):
    c, _ = private
    assert c.get("/").headers["location"] == "/login"  # "/" still leads to the login
    assert c.get("/about").status_code == 200  # but anyone may read what it is
    assert 'href="/about"' in c.get("/login").text
