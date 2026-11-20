"""The web app end to end: real app, real workers, simulated plug, mocked energy API."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from efficharge.devices.simulated import ALL_CAPS
from efficharge.web.auth import reset_limiters


def csrf(c: TestClient, path: str) -> str:
    return re.search(r'"X-CSRF-Token": "([^"]+)"', c.get(path).text).group(1)


def wait_for(c: TestClient, path: str, text: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(path).text
        if text in body:
            return body
        time.sleep(0.1)
    raise AssertionError(f"{text!r} not in {path} after {timeout}s:\n{body[:2000]}")


def test_landing_languages_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Charge your battery when electricity is cheap and green." in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    r = client.get("/?lang=de")
    assert "Lade deinen Akku, wenn Strom günstig und grün ist." in r.text
    assert 'lang="de"' in client.get("/").text  # the choice sticks in the session


def test_pages_need_login(client):
    r = client.get("/devices", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    r = client.get("/devices/1/status", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"


def start_demo(c: TestClient) -> int:
    r = c.get("/demo", follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def demo_action(c: TestClient, dev: int, action: str) -> str:
    token = csrf(c, f"/devices/{dev}")
    r = c.post(f"/devices/{dev}/demo", data={"action": action}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.text
    return r.text


def test_demo_dashboard_plug_in_and_button(client):
    dev = start_demo(client)
    body = client.get(f"/devices/{dev}").text
    assert "Virtual plug" in body
    assert "This is a virtual plug" in body
    assert "Create an account to connect your real plug" in body  # → register
    assert "Ready to charge" in body
    # no battery: only "plug in" is offered
    assert "Plug in a battery" in body
    assert "Press the plug button" not in body

    token = csrf(client, f"/devices/{dev}")
    assert client.post(f"/devices/{dev}/demo", data={"action": "plug_in"}).status_code == 403
    demo_action(client, dev, "plug_in")
    body = wait_for(client, f"/devices/{dev}/status", "Waiting for cheap, green power")
    assert "Waiting for the best window" in body
    assert "Charge now" in body
    # battery on: button and take-off, no second plug-in
    assert "Press the plug button" in body
    assert "Take the battery off" in body
    assert "Plug in a battery" not in body

    chart = client.get(f"/devices/{dev}/chart.json").json()
    assert [c["id"] for c in chart["charts"]] == ["green", "price", "power"]
    assert {b["kind"] for b in chart["bands"]} == {"plan", "baseline"}
    assert any(line["kind"] == "deadline" for line in chart["lines"])

    # the plug's own button: charge now, recorded as an override
    client.post(f"/devices/{dev}/demo", data={"action": "button"}, headers={"X-CSRF-Token": token})
    body = wait_for(client, f"/devices/{dev}/status", "with the plug button")
    assert "Stop charging" in body


def test_demo_fast_forward_and_back_to_now(client):
    dev = start_demo(client)
    body = demo_action(client, dev, "fast")
    assert "Normal speed" in body and "Back to now" in body
    assert 'hx-trigger="every 1s, resume"' in body  # status follows quickly while fast
    assert 'data-chart-refresh="2"' in body
    first = client.get(f"/devices/{dev}/chart.json").json()
    time.sleep(0.5)
    t2 = client.get(f"/devices/{dev}/chart.json").json()["now"]
    assert 4 * 60_000 < t2 - first["now"] < 40 * 60_000  # ~10 simulated minutes per 0.5 s
    # the browser glides "now" (and the time axis) along at this speed between refreshes
    assert first["clock"]["speed"] == 1200 and first["clock"]["until"] > first["now"]
    assert first["range_live"][0]  # the start; the end is fixed at the deadline here
    assert [(b["kind"], b["live"]) for b in first["bands"]] == [("next", [])]  # fixed: later

    body = demo_action(client, dev, "normal")
    assert "Fast-forward" in body
    assert client.get(f"/devices/{dev}/chart.json").json()["clock"]["speed"] == 1
    assert 'hx-trigger="every 2s, resume"' in body  # demos refresh quickly even at normal speed

    demo_action(client, dev, "plug_in")
    body = demo_action(client, dev, "reset")
    assert "Plug in a battery" in body and "Back to now" not in body
    now_line = client.get(f"/devices/{dev}/chart.json").json()["lines"][0]["x"]
    assert abs(now_line / 1000 - time.time()) < 5  # back to real time


def test_each_visitor_gets_their_own_demo(client, tmp_path):
    dev_a = start_demo(client)
    assert start_demo(client) == dev_a  # same visitor: same demo
    # a second visitor = same app and event loop, but without visitor A's cookie
    # (a second TestClient would run in a second event loop next to the same DB pool)
    cookies_a = dict(client.cookies)
    client.cookies.clear()
    dev_b = start_demo(client)
    assert dev_b != dev_a
    assert client.get(f"/devices/{dev_a}").status_code == 404
    client.cookies.clear()
    client.cookies.update(cookies_a)
    assert client.get(f"/devices/{dev_a}").status_code == 200


def pressed(page: str) -> str | None:
    """The choice the priority switch on a plug's page shows as selected."""
    m = re.search(r'name="priority" value="(\w+)" aria-pressed="true"', page)
    return m.group(1) if m else None


def test_priority_is_one_setting_for_all_plugs(client):
    dev = start_demo(client)
    token = csrf(client, f"/devices/{dev}")
    r = client.post(
        f"/devices/{dev}/priority",
        data={"priority": "eco", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert pressed(client.get(f"/devices/{dev}").text) == "eco"  # shown on the plug's page
    page = client.get("/settings").text
    assert "/settings/priority" not in page and "tariff" not in page  # no settings card
    # the same page as with an account: its virtual plug, but nothing it can't do (no
    # removing it, no real plug account, no account form: only language and "leave demo")
    assert 'id="plugs"' in page and "Virtual plug" in page
    assert "/delete" not in page and "/accounts/connect/" not in page
    assert "Create an account to connect your real plug" in page
    assert 'action="/settings/account"' not in page and "Leave demo" in page
    data = {"locale": "de", "timezone": "Europe/Berlin", "csrf_token": token}
    assert client.post("/settings/account", data=data).status_code == 403
    # old addresses lead to the settings page
    assert client.get("/accounts", follow_redirects=False).headers["location"] == "/settings#plugs"


def approve(client: TestClient, email: str) -> None:
    """What the admin does over SSH: ``efficharge approve EMAIL``."""
    from efficharge import users_cli

    rt = client.app.state.rt
    asyncio.run(users_cli.approve(rt.config.database_url, email))


def register(client: TestClient, email: str, password: str, virtual: bool = True) -> None:
    """Register, get accepted, log in; then (``virtual``) "Try a virtual plug first"."""
    token = csrf(client, "/register")
    data = {"email": email, "password": password, "about": "Tester", "csrf_token": token}
    assert "checked by hand" in client.post("/register", data=data).text
    approve(client, email)
    data = {"email": email, "password": password, "csrf_token": csrf(client, "/login")}
    assert client.post("/login", data=data, follow_redirects=False).status_code == 303
    if virtual:
        add_virtual_plug(client)


def add_virtual_plug(client: TestClient) -> int:
    """The button "Try a virtual plug first" (or "Add a virtual plug"): its dashboard."""
    r = client.post(
        "/accounts/virtual", data={"csrf_token": csrf(client, "/settings")}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/devices/")
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_registration_waits_for_approval_then_welcomes_in_the_settings(client):
    token = csrf(client, "/register")
    form = {"email": "a@example.org", "password": "long enough pw", "csrf_token": token}
    r = client.post("/register", data=form)
    assert r.status_code == 400 and "who you are" in r.text  # the text is required
    r = client.post("/register", data={**form, "about": "a neighbour, rides an e-bike"})
    assert r.status_code == 200 and "checked by hand" in r.text
    assert client.get("/devices", follow_redirects=False).status_code == 303  # not logged in

    login = {"email": "a@example.org", "password": "long enough pw"}
    r = client.post("/login", data={**login, "csrf_token": csrf(client, "/login")})
    assert r.status_code == 403 and "been accepted yet" in r.text

    from efficharge import users_cli

    url = client.app.state.rt.config.database_url
    waiting = asyncio.run(users_cli.pending(url))
    assert [(u.email, u.about) for u in waiting] == [
        ("a@example.org", "a neighbour, rides an e-bike")
    ]
    approve(client, "a@example.org")
    assert asyncio.run(users_cli.pending(url)) == []
    r = client.post(
        "/login", data={**login, "csrf_token": csrf(client, "/login")}, follow_redirects=False
    )
    assert r.status_code == 303
    # no plug yet: the dashboard leads to the settings, which welcome them
    assert client.get("/devices", follow_redirects=False).headers["location"] == "/settings"
    page = client.get("/settings").text
    assert "Welcome to efficharge!" in page and 'href="/accounts/connect/meross"' in page
    assert "Try a virtual plug first" in page
    assert "MSS310" in page  # which Meross plugs work best
    connect = client.get("/accounts/connect/meross").text
    assert "MSS310" in connect and "Meross email" in connect and "Meross password" in connect
    assert "Use at your own risk" in connect and "meross_iot</a>" in connect
    assert "mailto:" not in connect  # no address configured, no imprint either
    assert "<h2>Plugs</h2>" not in page  # nothing in it yet: the welcome is that section

    dev = add_virtual_plug(client)
    page = client.get(f"/devices/{dev}").text
    assert "This is a virtual plug" in page and 'href="/settings#plugs"' in page
    assert "Fast-forward" in page  # the same controls as the demo
    page = client.get("/settings").text
    assert "trying a virtual plug" in page and f'href="/devices/{dev}"' in page
    assert "Add a virtual plug" not in page  # one is enough (more over SSH)
    assert add_virtual_plug(client) == dev  # a second click: the same one


def test_register_login_logout_and_isolation(client):
    token = csrf(client, "/register")
    r = client.post(
        "/register",
        data={"email": "a@example.org", "password": "short", "about": "x", "csrf_token": token},
    )
    assert r.status_code == 400
    register(client, "A@Example.org", "long enough pw")
    assert "Virtual plug" in client.get("/devices").text
    # another user's device is invisible
    mine = dict(client.cookies)
    client.cookies.clear()
    demo_dev = start_demo(client)  # someone else's device
    client.cookies.clear()
    client.cookies.update(mine)
    assert client.get(f"/devices/{demo_dev}").status_code == 404
    assert client.get(f"/devices/{demo_dev}/chart.json").status_code == 404
    assert client.get("/stats").status_code == 200

    token = csrf(client, "/devices")
    client.post("/logout", data={"csrf_token": token})
    assert client.get("/devices", follow_redirects=False).status_code == 303

    token = csrf(client, "/login")
    r = client.post(
        "/login", data={"email": "a@example.org", "password": "wrong password", "csrf_token": token}
    )
    assert r.status_code == 400
    assert "Email or password is wrong." in r.text
    r = client.post(
        "/login",
        data={
            "email": "a@example.org",
            "password": "long enough pw",
            "csrf_token": token,
            "next": "//evil.example",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/devices"  # no open redirect


def test_login_rate_limit(client):
    token = csrf(client, "/login")
    codes = [
        client.post(
            "/login",
            data={"email": "x@example.org", "password": "nope nope nope", "csrf_token": token},
        ).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [400] * 10
    assert codes[10] == 429


def test_demo_user_cannot_log_in_with_password(client):
    token = csrf(client, "/login")
    r = client.post(
        "/login", data={"email": "demo@efficharge.invalid", "password": "", "csrf_token": token}
    )
    assert r.status_code in (400, 422)


def test_healthz(client):
    data = client.get("/healthz").json()
    assert data["ok"] is True
    assert data["runners"] == 0  # demos start on demand


def test_installable_web_app(client):
    page = client.get("/").text
    assert '<link rel="manifest"' in page
    assert 'name="theme-color"' in page
    manifest = client.get("/manifest.webmanifest?lang=de")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone"
    assert data["start_url"] == "/devices"
    assert data["lang"] == "de"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in data["icons"]}
    assert any(i.get("purpose") == "maskable" for i in data["icons"])
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200

    sw = client.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript")
    assert "__VERSION__" not in sw.text  # cache version filled in
    for path in re.findall(r'"(/(?:static/[^"]+|offline))"', sw.text):
        assert client.get(path).status_code == 200, path  # everything precached exists
    assert "Keine Verbindung" in client.get("/offline").text  # session is German now
    assert "No connection" in client.get("/offline?lang=en").text


def test_connect_meross_pick_plug_and_charge(client, monkeypatch):
    from efficharge.devices.meross import MerossAdapter
    from tests import fake_meross as fm

    plug = fm.FakeDevice("uuid-bike", "E-Bike Ladegerät", watts=0.0)
    attempts = []

    async def client_from_password(email, password, mfa):
        attempts.append(mfa)
        if mfa is None:
            raise fm.MissingMFA()
        return fm.FakeClient()

    async def connect(stored_auth):
        return fm.FakeManager([plug])

    monkeypatch.setattr(MerossAdapter, "client_from_password", staticmethod(client_from_password))
    monkeypatch.setattr(MerossAdapter, "connect_from_creds", staticmethod(connect))

    register(client, "rider@example.org", "long enough pw")
    assert "Connect your real plug" in client.get("/devices").text  # the virtual plug
    token = csrf(client, "/accounts/connect/meross")
    r = client.post(
        "/accounts/connect/meross",
        data={"email": "rider@example.org", "password": "meross-secret", "csrf_token": token},
    )
    assert r.status_code == 400
    assert "two-factor authentication" in r.text
    assert 'name="mfa_code"' in r.text and "required autofocus" in r.text
    r = client.post(
        "/accounts/connect/meross",
        data={
            "email": "rider@example.org",
            "password": "meross-secret",
            "mfa_code": "123456",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("/pick")
    assert attempts == [None, "123456"]

    accounts_page = client.get("/accounts").text
    assert "Connected as rider@example.org" in accounts_page
    assert "Connect another account" in accounts_page
    pick = client.get(r.headers["location"]).text
    assert "E-Bike Ladegerät" in pick
    assert "plugging in is detected automatically" in pick
    r = client.post(
        r.headers["location"],
        data={"vendor_device_id": "uuid-bike", "default_hours": "3", "csrf_token": token},
        follow_redirects=False,
    )
    assert "welcome=1" in r.headers["location"]
    dev = int(r.headers["location"].split("/")[2])
    settings = client.get(r.headers["location"]).text
    assert "Your plug is connected." in settings
    assert "3 h" in settings  # the 3 h estimate, as is

    # the stored account holds the token, never the password
    async def stored_auth():
        from efficharge.db.models import DeviceAccount
        from efficharge.db.repo import account_auth

        rt = client.app.state.rt
        async with rt.sessions() as s:
            account = (
                (await s.execute(select(DeviceAccount).where(DeviceAccount.kind == "meross")))
                .scalars()
                .one()
            )
            return account.id, account.auth_encrypted, account_auth(rt.vault, account)

    meross_id, encrypted, auth_data = client.portal.call(stored_auth)
    assert "meross-secret" not in encrypted and "tok-123" not in encrypted
    assert auth_data["token"] == "tok-123" and "meross-secret" not in repr(auth_data)

    # plug in the charger: the plug measures 120 W, efficharge plans and switches off
    plug.watts = 120.0
    body = wait_for(client, f"/devices/{dev}/status", 'class="done"')  # "ready" passed
    # Which decision is right depends on the wall clock (default rule: ready by 17:00);
    # either way the plug's relay must match it.
    if "status-planned" in body:
        assert "Charge now" in body
        assert plug.is_on() is False
    else:
        assert "status-charging" in body
        assert plug.is_on() is True
    assert "Plug offline" not in body

    # the plug goes offline: no more readings, the status says so
    plug.online_status = fm.OnlineStatus.OFFLINE

    async def last_read(ago):
        from efficharge.db.models import DeviceStateRow

        async with client.app.state.rt.sessions() as s, s.begin():
            row = await s.get(DeviceStateRow, dev)
            row.last_seen = datetime.now(UTC) - ago

    client.portal.call(last_read, timedelta(minutes=10))
    assert "Plug offline since" in client.get(f"/devices/{dev}/status").text

    # removing the account removes its plugs
    token = csrf(client, "/accounts")
    client.post(f"/accounts/{meross_id}/delete", data={"csrf_token": token})
    assert client.get(f"/devices/{dev}").status_code == 404


def test_battery_capacity_in_wh_or_volts_times_amp_hours():
    from efficharge.web.app import battery_capacity_kwh

    assert battery_capacity_kwh("", "36", "14") == 0.504  # what's printed on the battery
    assert battery_capacity_kwh("", "36,0", "14,5") == 0.522
    assert battery_capacity_kwh("504", "", "") == 0.504
    assert battery_capacity_kwh("504", "36", "") == 0.504  # incomplete V × Ah: use Wh
    assert battery_capacity_kwh("", "", "") is None
    assert battery_capacity_kwh("abc", "", "") is None


def test_battery_size_only_for_plugs_without_metering(client):
    dev = start_demo(client)
    assert 'name="battery_wh"' not in client.get(f"/devices/{dev}/settings").text  # metered

    async def unmetered():
        from efficharge.db.models import DeviceSettings

        async with client.app.state.rt.sessions() as s, s.begin():
            (await s.get(DeviceSettings, dev)).metered = False

    client.portal.call(unmetered)
    token = csrf(client, f"/devices/{dev}/settings")
    client.post(
        f"/devices/{dev}/settings",
        data={"default_hours": "3", "battery_v": "36", "battery_ah": "14", "csrf_token": token},
    )
    page = client.get(f"/devices/{dev}/settings").text
    assert 'name="battery_wh" placeholder="504" value="504"' in page


def test_rules_add_edit_delete_with_htmx(client):
    dev = start_demo(client)
    page = client.get(f"/devices/{dev}/settings").text
    assert 'type="time"' not in page  # 24 h selects, not the browser's AM/PM time input
    assert '<option value="17:00"' in page
    # reached with the gear on the dashboard: the menu says "Dashboard", not "Settings"
    assert re.search(r'<a href="/devices"\s+aria-current="page"', page)
    assert '<a href="/settings" aria-current' not in page
    token = csrf(client, f"/devices/{dev}/settings")
    hx = {"X-CSRF-Token": token, "HX-Request": "true"}

    r = client.post(
        f"/devices/{dev}/rules",
        headers=hx,
        data={
            "wd": ["0", "1", "2", "3", "4"],
            "plugged_from": "06:00",
            "plugged_until": "15:00",
            "ready_by": "17:00",
        },
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<div id="rules-list">')  # only the list, no full page
    assert "ready by 17:00" in r.text
    rule_id = re.search(r"/rules/(\d+)/delete", r.text).group(1)

    r = client.post(
        f"/devices/{dev}/rules/{rule_id}",
        headers=hx,
        data={
            "wd": ["5", "6"],
            "plugged_from": "00:00",
            "plugged_until": "00:00",
            "ready_by": "09:30",
        },
    )
    assert "ready by 09:30" in r.text and "ready by 17:00" not in r.text

    # a "ready within" rule
    r = client.post(
        f"/devices/{dev}/rules/{rule_id}",
        headers=hx,
        data={
            "plugged_from": "08:00",
            "plugged_until": "18:00",
            "target": "within",
            "within_h": "6",
        },
    )
    assert "08:00–18:00 → ready within 6 h" in r.text

    r = client.post(f"/devices/{dev}/rules/{rule_id}/delete", headers=hx)
    assert f"/rules/{rule_id}" not in r.text
    # the fallback rule is always there, can't be deleted, only its time changes
    assert "Otherwise → ready within 12 h" in r.text and "/rules/fallback/delete" not in r.text
    r = client.post(f"/devices/{dev}/rules/fallback", headers=hx, data={"within_h": "8"})
    assert "Otherwise → ready within 8 h" in r.text
    assert (
        client.post(
            f"/devices/{dev}/rules/fallback", headers=hx, data={"within_h": "7"}
        ).status_code
        == 400
    )
    assert (
        client.post(f"/devices/{dev}/rules", headers=hx, data={"ready_by": "7:5"}).status_code
        == 400
    )


def test_ready_by_with_day_and_time_selects(client):
    dev = start_demo(client)
    demo_action(client, dev, "plug_in")
    body = wait_for(client, f"/devices/{dev}/status", 'name="ready_day"')
    assert 'type="datetime-local"' not in body
    day = re.search(r'name="ready_day".*?<option value="([^"]+)"', body, re.S).group(1)
    token = csrf(client, f"/devices/{dev}")
    r = client.post(
        f"/devices/{dev}/command",
        headers={"X-CSRF-Token": token},
        data={"action": "ready_by", "ready_day": day, "ready_time": "23:45"},
    )
    assert r.status_code == 200


def test_static_files_have_versioned_urls(client):
    from efficharge.web.app import STATIC_VERSION

    page = client.get("/").text
    assert f'href="/static/app.css?v={STATIC_VERSION}"' in page
    assert client.get(f"/static/app.css?v={STATIC_VERSION}").status_code == 200
    sw = client.get("/sw.js").text
    assert "network first" in sw.lower() or "Everything comes from the network first" in sw


def test_refresh_button_reads_the_plug_now(client):
    dev = start_demo(client)
    demo_action(client, dev, "plug_in")
    wait_for(client, f"/devices/{dev}/status", "Waiting for")
    token = csrf(client, f"/devices/{dev}")
    rt = client.app.state.rt

    async def last_seen():
        from efficharge.db.models import DeviceStateRow

        async with rt.sessions() as s:
            return (await s.get(DeviceStateRow, dev)).last_seen

    before = client.portal.call(last_seen)
    r = client.post(
        f"/devices/{dev}/command", data={"action": "refresh"}, headers={"X-CSRF-Token": token}
    )
    assert r.status_code == 200
    assert client.portal.call(last_seen) > before  # a fresh reading was stored


def test_chart_names_the_rule_behind_the_deadline(client):
    dev = start_demo(client)
    token = csrf(client, f"/devices/{dev}/settings")
    hx = {"X-CSRF-Token": token, "HX-Request": "true"}
    r = client.post(
        f"/devices/{dev}/rules",
        headers=hx,
        data={
            "wd": [str(d) for d in range(7)],
            "plugged_from": "00:00",
            "plugged_until": "00:00",
            "ready_by": "23:45",
        },
    )
    rule_id = re.search(r'id="rule-(\d+)"', r.text).group(1)

    demo_action(client, dev, "plug_in")
    # planned, or (late in the evening: too little time left) charging right away
    wait_for(client, f"/devices/{dev}/status", 'class="done"')
    chart = client.get(f"/devices/{dev}/chart.json").json()
    deadline = next(line for line in chart["lines"] if line["kind"] == "deadline")
    assert deadline["label"] == "ready by 23:45 · rule 1"
    href = f"/devices/{dev}/settings?edit={rule_id}#rule-{rule_id}"
    status = client.get(f"/devices/{dev}/status").text
    assert (
        f'<p class="rule-in-force">Rule: <a href="{href}">Any time → ready by 23:45</a>' in status
    )
    page = client.get(href).text
    assert f'<li id="rule-{rule_id}">' in page
    assert re.search(r'<details class="rule-edit"\s+open>', page)  # opens right at that rule


def test_rule_is_shown_even_before_plug_in(client):
    dev = start_demo(client)
    chart = client.get(f"/devices/{dev}/chart.json").json()
    deadline = next(line for line in chart["lines"] if line["kind"] == "deadline")
    assert deadline["label"].endswith("· default rule")
    status = client.get(f"/devices/{dev}/status").text
    href = f"/devices/{dev}/settings?edit=fallback#rule-fallback"
    assert f'<a href="{href}">Otherwise → ready within 12 h</a>' in status
    assert chart["range"][1] >= deadline["x"]  # the deadline is inside the chart


def test_charges_list_includes_running_charge_and_can_delete_finished(client):
    dev = start_demo(client)
    demo_action(client, dev, "button")  # plug off …
    demo_action(client, dev, "button")  # … and on again: nothing to charge yet
    demo_action(client, dev, "plug_in")
    wait_for(client, f"/devices/{dev}/status", "status-planned")
    page = client.get("/stats").text
    assert "Waiting until" in page  # the planned charge is listed, not deletable yet
    assert "/delete" not in page

    async def finish_it():
        from efficharge.db.models import Cycle

        async with client.app.state.rt.sessions() as s, s.begin():
            cycle = (await s.execute(select(Cycle))).scalars().one()
            done = Cycle(
                device_id=cycle.device_id,
                plugged_at=cycle.plugged_at,
                actual_start=cycle.plugged_at,
                end_at=cycle.plugged_at,
                kwh=0.2,
                end_reason="full",
                plan=cycle.plan,  # a real stored plan
            )
            s.add(done)
            await s.flush()
            return done.id

    done_id = client.portal.call(finish_it)
    page = client.get("/stats").text
    assert f"/cycles/{done_id}/delete" in page
    token = csrf(client, "/stats")
    assert "Delete the charge from" in page and "(200 Wh) from the statistics?" in page
    r = client.post(f"/cycles/{done_id}/delete", headers={"X-CSRF-Token": token})
    assert r.headers.get("hx-refresh") == "true"
    page = client.get("/stats").text
    assert f"/cycles/{done_id}/delete" not in page
    assert f"/cycles/{done_id}/restore" in page  # undo offered
    client.post(f"/cycles/{done_id}/restore", headers={"X-CSRF-Token": token})
    page = client.get("/stats").text
    assert f"/cycles/{done_id}/delete" in page  # back again
    assert "/restore" not in page


def test_idle_status_previews_the_window_and_caches_it(client):
    from efficharge.web import data

    dev = start_demo(client)
    # fake prices: expensive now, cheap from +4 h → a real window is chosen
    status = client.get(f"/devices/{dev}/status").text
    assert "Charging would start at" in status
    assert pressed(status) == "balanced"  # the default, shown as the switch on the card
    # the chart outlines the same window (not as a planned charge)
    chart = client.get(f"/devices/{dev}/chart.json").json()
    assert [b["kind"] for b in chart["bands"]] == ["next"]
    assert {line["kind"] for line in chart["lines"]} == {"now", "deadline"}
    first = data._previews[dev]
    client.get(f"/devices/{dev}/status")
    assert data._previews[dev] is first  # same inputs: not recomputed

    token = csrf(client, f"/devices/{dev}/settings")
    client.post(
        f"/devices/{dev}/rules",
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
        data={
            "wd": [str(d) for d in range(7)],
            "plugged_from": "00:00",
            "plugged_until": "00:00",
            "ready_by": "23:45",
        },
    )
    client.get(f"/devices/{dev}/status")
    assert data._previews[dev] is not first  # a rule changed: recomputed


def test_the_last_charge_stays_in_the_chart_as_long_as_below_the_status(client):
    from efficharge.db.models import Cycle

    dev = start_demo(client)
    now = client.app.state.rt.clock_for(dev)()

    async def add_charge(ended_ago):
        async with client.app.state.rt.sessions() as s, s.begin():
            end = now - ended_ago
            s.add(
                Cycle(
                    device_id=dev,
                    plugged_at=end - timedelta(hours=3),
                    actual_start=end - timedelta(hours=2),
                    end_at=end,
                    kwh=0.3,
                    end_reason="full",
                )
            )

    client.portal.call(add_charge, timedelta(hours=11))
    chart = client.get(f"/devices/{dev}/chart.json").json()
    charging = next(b for b in chart["bands"] if b["kind"] == "charging")
    assert charging["to"] == int((now - timedelta(hours=11)).timestamp() * 1000)
    assert chart["range"][0] <= charging["from"]  # the whole charge is in view
    assert "Last charge" in client.get(f"/devices/{dev}/status").text


def test_chart_keeps_the_history_while_charging(client):
    dev = start_demo(client)
    demo_action(client, dev, "plug_in")
    wait_for(client, f"/devices/{dev}/status", "status-planned")
    token = csrf(client, f"/devices/{dev}")
    client.post(
        f"/devices/{dev}/command", data={"action": "charge_now"}, headers={"X-CSRF-Token": token}
    )
    wait_for(client, f"/devices/{dev}/status", "status-charging")
    chart = client.get(f"/devices/{dev}/chart.json").json()
    charging = next(b for b in chart["bands"] if b["kind"] == "charging")
    power = next(c for c in chart["charts"] if c["id"] == "power")
    assert power["suggested_max"] >= 100  # this plug's charging power, not the 50 W default
    # fixed at the plug-in: the "plugged in" line, and "if charged right away" from there
    plugin = next(line for line in chart["lines"] if line["kind"] == "plugin")
    assert plugin["label"].startswith("plugged in")
    baseline = next(b for b in chart["bands"] if b["kind"] == "baseline")
    assert baseline["from"] == plugin["x"]
    # the chart starts 2 h before the plug-in, so the charge so far is visible
    assert chart["range"][0] <= charging["from"] - 2 * 3600 * 1000 + 60_000
    # one after another: charged → expected rest (at least 5 min) → rest of the window
    rest = next(b for b in chart["bands"] if b["kind"] == "remaining")
    assert rest["from"] == charging["to"] and rest["to"] - rest["from"] >= 5 * 60_000
    window = [b for b in chart["bands"] if b["kind"] == "plan"]
    assert all(b["from"] >= rest["to"] for b in window)


def test_top_bar_has_three_places_and_logout_is_in_the_settings(client):
    dev = start_demo(client)
    page = client.get(f"/devices/{dev}").text
    nav = page.split("<nav>")[1].split("</nav>")[0]
    places = [h for h in re.findall(r'<a href="([^"]+)"', nav) if not h.startswith("?lang")]
    assert places == ["/devices", "/stats", "/settings"]
    assert "/logout" not in nav
    settings = client.get("/settings").text
    assert 'action="/logout"' in settings and "?lang=de" in settings  # demo: language links


def test_settings_save_on_change(client):
    dev = start_demo(client)
    token = csrf(client, f"/devices/{dev}/settings")
    hx = {"X-CSRF-Token": token, "HX-Request": "true"}
    r = client.post(f"/devices/{dev}/settings", headers=hx, data={"default_hours": "2.5"})
    assert r.status_code == 200 and 'id="planned"' in r.text and "2 h 30 min" in r.text
    # an in-between value (from the old fine-tuning slider) shows no choice as selected
    client.portal.call(_set_weight, client, 0.35)
    page = client.get(f"/devices/{dev}").text
    assert pressed(page) is None and "Custom setting" in page


async def _set_weight(client, weight: float) -> None:
    from efficharge.db.models import User

    async with client.app.state.rt.sessions() as s, s.begin():
        for user in (await s.scalars(select(User))).all():
            user.price_weight = weight


def test_language_from_the_top_bar_is_stored_like_in_the_settings(client):
    start_demo(client)
    client.get("/settings?lang=de")
    page = client.get("/settings", headers={"accept-language": "en"}).text
    assert 'lang="de"' in page  # stays German: stored for the user, not only this request


def test_power_axis_without_a_charge_is_not_zoomed_into_standby_noise(client):
    dev = start_demo(client)
    power = next(
        c for c in client.get(f"/devices/{dev}/chart.json").json()["charts"] if c["id"] == "power"
    )
    assert power["suggested_max"] == 50.0  # no charge yet


def test_virtual_charges_count_only_without_a_real_plug(client):
    from efficharge.db.models import Cycle, Device, User
    from efficharge.db.repo import add_account, add_device

    register(client, "v@example.org", "long enough pw")
    rt = client.app.state.rt

    async def add_charge():
        async with rt.sessions() as s, s.begin():
            plug = (await s.scalars(select(Device))).one()  # the virtual plug
            end = datetime.now(UTC) - timedelta(days=1)
            s.add(
                Cycle(
                    device_id=plug.id,
                    plugged_at=end - timedelta(hours=3),
                    actual_start=end - timedelta(hours=3),
                    end_at=end,
                    kwh=0.3,
                    end_reason="full",
                )
            )

    async def add_real_plug():
        async with rt.sessions() as s, s.begin():
            user = await s.scalar(select(User).where(User.email == "v@example.org"))
            account = await add_account(s, rt.vault, user, "meross", {"token": "t"})
            await add_device(
                s, user, "Bike", account=account, vendor_device_id="m-1", capabilities=ALL_CAPS
            )

    client.portal.call(add_charge)
    assert "300 Wh" in client.get("/stats").text  # trying it out: its charges are shown
    client.portal.call(add_real_plug)
    assert "300 Wh" not in client.get("/stats").text  # now only the real plug counts


def test_several_plugs_are_tabs_and_the_dashboard_opens_the_last_one(client):
    from efficharge.db.models import Device, User
    from efficharge.db.repo import add_account, add_device

    register(client, "t@example.org", "long enough pw")
    rt = client.app.state.rt

    async def add_real_plug():
        async with rt.sessions() as s, s.begin():
            user = await s.scalar(select(User).where(User.email == "t@example.org"))
            account = await add_account(s, rt.vault, user, "meross", {"token": "t"})
            real = await add_device(
                s, user, "Bike", account=account, vendor_device_id="m-1", capabilities=ALL_CAPS
            )
            virtual = await s.scalar(select(Device).where(Device.name == "Virtual plug"))
            return real.id, virtual.id

    real, virtual = client.portal.call(add_real_plug)
    page = client.get(f"/devices/{virtual}").text
    tabs = re.search(r'<nav class="plug-tabs".*?</nav>', page, re.S).group(0)
    assert tabs.index("Bike") < tabs.index("Virtual plug")  # real plugs first
    assert re.search(rf'href="/devices/{virtual}"\s+aria-current="page">Virtual plug', tabs)
    assert f'href="/devices/{real}" aria-current' not in tabs
    # "Dashboard" opens the plug looked at last, no list in between
    r = client.get("/devices", follow_redirects=False)
    assert r.headers["location"] == f"/devices/{virtual}"


def test_users_delete_their_own_account(client):
    import sqlite3

    register(client, "gone@example.org", "long enough pw")
    register(client, "stays@example.org", "long enough pw")
    stays = dict(client.cookies)
    client.cookies.clear()
    data = {"email": "gone@example.org", "password": "long enough pw"}
    client.post("/login", data={**data, "csrf_token": csrf(client, "/login")})
    dev = int(client.get("/devices", follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    assert client.get(f"/devices/{dev}").status_code == 200
    db_file = client.app.state.rt.config.database_url.split("///", 1)[1]

    def rows(sql, *args):
        with sqlite3.connect(db_file) as con:
            return con.execute(sql, args).fetchone()[0]

    uid = rows("SELECT id FROM users WHERE email = ?", "gone@example.org")
    assert rows("SELECT count(*) FROM device_accounts WHERE user_id = ?", uid) >= 1
    assert "Delete account" in client.get("/settings").text

    # a wrong password deletes nothing
    token = csrf(client, "/settings")
    r = client.post("/settings/delete", data={"password": "not my password", "csrf_token": token})
    assert "The password is wrong. Nothing was deleted." in r.text
    assert rows("SELECT count(*) FROM users WHERE id = ?", uid) == 1

    r = client.post(
        "/settings/delete",
        data={"password": "long enough pw", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/login?deleted=1"
    assert "all its data have been deleted" in client.get("/login?deleted=1").text
    assert client.get("/devices", follow_redirects=False).status_code == 303  # logged out
    assert dev not in client.app.state.rt.supervisor.runners  # its plug was stopped
    assert rows("SELECT count(*) FROM users WHERE id = ?", uid) == 0
    for table in ("device_accounts", "devices"):
        assert rows(f"SELECT count(*) FROM {table} WHERE user_id = ?", uid) == 0, table
    assert rows("SELECT count(*) FROM devices WHERE id = ?", dev) == 0
    for table in ("cycles", "power_samples", "deadline_rules", "device_settings"):
        assert rows(f"SELECT count(*) FROM {table} WHERE device_id = ?", dev) == 0, table
    r = client.post("/login", data={**data, "csrf_token": csrf(client, "/login")})
    assert r.status_code == 400  # the login is gone too

    client.cookies.clear()
    client.cookies.update(stays)  # the other user is untouched, still logged in
    assert (
        client.get("/devices", follow_redirects=False).headers["location"].startswith("/devices/")
    )
    assert rows("SELECT count(*) FROM users WHERE email = ?", "stays@example.org") == 1


def test_demo_cannot_delete_an_account(client):
    dev = start_demo(client)
    assert "Delete account" not in client.get("/settings").text
    token = csrf(client, f"/devices/{dev}")
    r = client.post("/settings/delete", data={"password": "", "csrf_token": token})
    assert r.status_code in (403, 422)


def test_start_page_shows_an_example_charge(client):
    page = client.get("/?lang=en").text
    assert "An example charge" in page and 'data-url="/example-chart.json"' in page
    assert "Waited 3:30 h" in page and "52 g CO₂ avoided" in page  # the usual charge labels
    assert "dynamic tariff" in page  # cost only counts with one
    data = client.get("/example-chart.json").json()  # the same shape as a plug's chart.json
    assert [c["id"] for c in data["charts"]] == ["green", "price", "power"]
    assert all(len(c["points"]) > 10 for c in data["charts"])
    assert {b["kind"] for b in data["bands"]} == {"baseline", "charging"}
    assert data["lines"][0]["label"] == "plugged in 08:00"
    assert data["range"][0] < data["lines"][0]["x"] < data["lines"][1]["x"] < data["range"][1]
    client.get("/?lang=de")  # the labels follow the visitor's language
    assert client.get("/example-chart.json").json()["lines"][0]["label"] == "eingesteckt 08:00"
    assert "<h2>Beispiel</h2>" in client.get("/").text


def test_priority_switch_on_the_plug_page_replans_right_away(client):
    dev = start_demo(client)
    page = client.get(f"/devices/{dev}").text
    assert pressed(page) == "balanced" and "Eco first" in page and "Cost first" in page
    token = csrf(client, f"/devices/{dev}")
    hx = {"X-CSRF-Token": token, "HX-Request": "true"}
    r = client.post(f"/devices/{dev}/priority", headers=hx, data={"priority": "eco"})
    assert r.status_code == 200 and 'id="status"' in r.text  # the card, re-planned
    assert pressed(r.text) == "eco"
    assert pressed(client.get(f"/devices/{dev}/status").text) == "eco"  # stored
    r = client.post(  # without JavaScript: a normal form post
        f"/devices/{dev}/priority",
        data={"priority": "money", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/devices/{dev}"
    assert pressed(client.get(f"/devices/{dev}").text) == "money"
    assert (
        client.post(f"/devices/{dev}/priority", headers=hx, data={"priority": "x"}).status_code
        == 400
    )
    # someone else's plug: not found
    assert (
        client.post(
            f"/devices/{dev + 999}/priority", headers=hx, data={"priority": "eco"}
        ).status_code
        == 404
    )


def test_an_ended_demo_starts_a_new_one_instead_of_the_login(client):
    """Demo users are deleted after 2 h idle and at every restart. Their visitor has no
    login: the next page or status refresh starts a new demo instead."""
    dev = start_demo(client)
    manager = client.app.state.rt.demo
    client.portal.call(manager.delete, manager.demos[dev])  # what a restart does
    r = client.get(f"/devices/{dev}/status", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/demo"
    dev2 = start_demo(client)  # the browser follows: a fresh demo
    assert client.get(f"/devices/{dev2}").status_code == 200
    manager_demo = manager.demos[dev2]
    client.portal.call(manager.delete, manager_demo)
    assert client.get("/").status_code == 200  # a public page first: the signal stays
    r = client.get("/stats", follow_redirects=False)  # a normal page load
    assert r.status_code == 303 and r.headers["location"] == "/demo"


def test_an_old_demo_cookie_never_opens_someone_elses_demo(client):
    """SQLite reuses the ids of deleted users: the cookie of a deleted demo user must not
    fit the next demo user who gets the same id."""
    dev_a = start_demo(client)
    cookies_a = dict(client.cookies)
    manager = client.app.state.rt.demo
    uid_a = manager.demos[dev_a].user_id
    client.portal.call(manager.delete, manager.demos[dev_a])  # a restart
    client.cookies.clear()
    dev_b = start_demo(client)  # the next visitor
    assert manager.demos[dev_b].user_id == uid_a  # same id again (that's the risk)
    client.cookies.clear()
    client.cookies.update(cookies_a)
    r = client.get(f"/devices/{dev_b}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/demo"  # not B's plug
    r = client.get("/demo", follow_redirects=False)  # a new demo of their own
    assert r.headers["location"] != f"/devices/{dev_b}"


def test_a_virtual_plug_in_fast_forward_is_never_offline(client):
    """At 1200× a reading every 0.1 s is minutes on the plug's clock: no offline warning,
    and "updated … ago" counts real time."""
    dev = start_demo(client)
    demo_action(client, dev, "fast")
    manager = client.app.state.rt.demo
    clock = manager.demos[dev].clock

    async def last_seen_an_hour_ago() -> None:
        from efficharge.db.models import DeviceStateRow

        async with client.app.state.rt.sessions() as s, s.begin():
            row = await s.get(DeviceStateRow, dev)
            row.last_seen = clock() - timedelta(hours=1)
            row.last_power_w = 0.0

    client.portal.call(last_seen_an_hour_ago)
    status = client.get(f"/devices/{dev}/status").text
    assert "Plug offline" not in status
    assert "updated 0 min ago" in status  # an hour at 1200× is 3 s


def connect_meross(client, monkeypatch, email: str, *plugs) -> tuple[dict[str, int], str]:
    """Register, connect a fake Meross account with these plugs, add them all.
    Returns {uuid: device id} and the CSRF token."""
    from efficharge.devices.meross import MerossAdapter
    from tests import fake_meross as fm

    async def client_from_password(email, password, mfa):
        return fm.FakeClient()

    async def connect(stored_auth):
        return fm.FakeManager(list(plugs))

    monkeypatch.setattr(MerossAdapter, "client_from_password", staticmethod(client_from_password))
    monkeypatch.setattr(MerossAdapter, "connect_from_creds", staticmethod(connect))
    register(client, email, "long enough pw")
    token = csrf(client, "/accounts/connect/meross")
    login = {"email": email, "password": "x", "csrf_token": token}
    pick = client.post("/accounts/connect/meross", data=login, follow_redirects=False)
    devices = {}
    for plug in plugs:
        r = client.post(
            pick.headers["location"],
            data={"vendor_device_id": plug.uuid, "default_hours": "3", "csrf_token": token},
            follow_redirects=False,
        )
        devices[plug.uuid] = int(r.headers["location"].split("/")[2])
    return devices, token


def test_remove_one_plug_switches_it_on_and_keeps_the_others(client, monkeypatch):
    from tests import fake_meross as fm

    bike = fm.FakeDevice("uuid-bike", "E-Bike", watts=0.0)
    drill = fm.FakeDevice("uuid-drill", "Werkzeug", watts=0.0)
    devices, token = connect_meross(client, monkeypatch, "two@example.org", bike, drill)
    bike_id, drill_id = devices["uuid-bike"], devices["uuid-drill"]
    page = client.get(f"/devices/{bike_id}/settings").text
    assert f'action="/devices/{bike_id}/remove"' in page

    bike._on = False  # waiting for its window: switched off
    r = client.post(
        f"/devices/{bike_id}/remove", data={"csrf_token": token}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings?removed=on#plugs"
    assert bike.is_on() is True  # released: never left off with a battery waiting
    assert "It is switched on" in client.get("/settings?removed=on").text
    assert client.get(f"/devices/{bike_id}").status_code == 404
    assert client.get(f"/devices/{drill_id}").status_code == 200  # the other one stays
    assert "Connected as" in client.get("/settings").text  # and so does the account

    # the virtual plug belongs to the account: no remove button, and the route refuses
    virtual = next(
        int(m)
        for m in re.findall(r'href="/devices/(\d+)"', client.get("/settings").text)
        if int(m) not in (bike_id, drill_id)
    )
    assert "/remove" not in client.get(f"/devices/{virtual}/settings").text
    r = client.post(f"/devices/{virtual}/remove", data={"csrf_token": token})
    assert r.status_code == 403

    # removing the whole Meross account releases its plugs the same way
    async def meross_account_id() -> int:
        from efficharge.db.models import DeviceAccount

        async with client.app.state.rt.sessions() as s:
            kind = DeviceAccount.kind == "meross"
            return (await s.scalars(select(DeviceAccount.id).where(kind))).one()

    drill._on = False
    account_id = client.portal.call(meross_account_id)
    client.post(f"/accounts/{account_id}/delete", data={"csrf_token": token})
    assert drill.is_on() is True
    assert client.get(f"/devices/{drill_id}").status_code == 404


def test_removing_a_plug_that_cant_be_switched_on_says_so(client, monkeypatch):
    from tests import fake_meross as fm

    bike = fm.FakeDevice("uuid-bike", "E-Bike", watts=0.0)
    devices, token = connect_meross(client, monkeypatch, "off@example.org", bike)
    dev = devices["uuid-bike"]
    bike._on = False
    bike.fail_with = TimeoutError("the cloud doesn't answer")  # switching on fails
    r = client.post(f"/devices/{dev}/remove", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"] == "/settings?removed=off#plugs"  # removed, but not on
    page = client.get("/settings?removed=off").text
    assert "couldn't switch it on" in page and "It is switched on" not in page
    assert client.get(f"/devices/{dev}").status_code == 404


def test_about_is_the_start_page_for_everyone(client):
    page = client.get("/?lang=en").text
    assert "Use at your own risk" in page
    assert "Use at your own risk" in client.get("/register").text
    assert 'href="/about"' in page and "About efficharge" in page  # top bar and footer
    assert "every registration is checked by hand" in page  # what happens after signing up
    register(client, "about@example.org", "long enough pw")
    assert client.get("/", follow_redirects=False).headers["location"] == "/devices"
    about = client.get("/about")  # logged in: still the start page
    assert about.status_code == 200 and "An example charge" in about.text
    assert 'class="wide-only" href="/about" aria-current="page"' in about.text


def test_bots_cant_use_up_the_demo(client):
    for _ in range(3):  # three demos per address and hour
        client.cookies.clear()
        start_demo(client)
    client.cookies.clear()
    r = client.get("/demo?lang=en", follow_redirects=False)
    assert r.status_code == 429 and "That's a lot of demos" in r.text


def test_untouched_public_demos_go_after_15_minutes(client):
    dev = start_demo(client)
    manager = client.app.state.rt.demo
    manager.demos[dev].last_seen -= 16 * 60  # no request for 16 min: the visitor left
    client.portal.call(manager.step)
    assert dev not in manager.demos


def test_registration_honeypot_limit_and_cap(client, monkeypatch):
    from efficharge.web import app as web_app

    def form(n, **extra):
        token = csrf(client, "/register")
        data = {"email": f"r{n}@example.org", "password": "long enough pw", "about": "me"}
        return client.post("/register", data={**data, "csrf_token": token, **extra})

    url = client.app.state.rt.config.database_url
    from efficharge import users_cli

    # a bot fills in the hidden field: it sees "done", nothing is stored
    r = form(0, website="https://spam.example")
    assert "checked by hand" in r.text
    assert asyncio.run(users_cli.pending(url)) == []
    assert (
        'name="website"' in client.get("/register").text
        and 'class="hp"' in client.get("/register").text
    )

    # three per address and hour; a mistake (here: a short password) doesn't count
    token = csrf(client, "/register")
    bad = {"email": "typo@example.org", "password": "short", "about": "me", "csrf_token": token}
    assert client.post("/register", data=bad).status_code == 400
    assert all("checked by hand" in form(n).text for n in (1, 2, 3))
    r = form(4)
    assert r.status_code == 400 and "Too many registrations" in r.text

    # at most MAX_PENDING waiting, whatever the address
    reset_limiters()
    monkeypatch.setattr(web_app, "MAX_PENDING", 3)
    r = form(5)
    assert r.status_code == 400 and "Registration is full right now" in r.text


def test_settings_say_where_you_stand(client, monkeypatch):
    from efficharge.db.models import DeviceAccount
    from tests import fake_meross as fm

    bike = fm.FakeDevice("uuid-bike", "E-Bike", watts=0.0)
    devices, token = connect_meross(client, monkeypatch, "state@example.org", bike)
    page = client.get("/settings").text
    assert "All set" in page and "Efficharge switches your plug." in page
    assert "Welcome to efficharge!" not in page and "<h2>Plugs</h2>" in page

    client.post(f"/devices/{devices['uuid-bike']}/remove", data={"csrf_token": token})
    page = client.get("/settings").text  # the account stays, without a plug
    assert "Next: pick your plug" in page and re.search(r'href="/accounts/\d+/pick"', page)

    async def expire() -> None:
        async with client.app.state.rt.sessions() as s, s.begin():
            account = await s.scalar(select(DeviceAccount).where(DeviceAccount.kind == "meross"))
            account.status = "needs_reauth"

    client.portal.call(expire)
    page = client.get("/settings").text
    assert "Please log in again" in page and "/accounts/connect/meross?account=" in page


def test_picker_lists_only_plugs_and_greys_out_those_without_metering(client, monkeypatch):
    from efficharge.devices.meross import MerossAdapter
    from tests import fake_meross as fm

    bike = fm.FakeDevice("uuid-bike", "E-Bike", watts=0.0)
    old = fm.FakeDevice("uuid-old", "Old plug", abilities={"Appliance.Control.ToggleX": {}})
    lamp = fm.FakeDevice("uuid-lamp", "Living room lamp", type="msl120")

    async def client_from_password(email, password, mfa):
        return fm.FakeClient()

    devices = [old, bike, lamp]

    async def connect(stored_auth):
        return fm.FakeManager(devices)

    monkeypatch.setattr(MerossAdapter, "client_from_password", staticmethod(client_from_password))
    monkeypatch.setattr(MerossAdapter, "connect_from_creds", staticmethod(connect))
    register(client, "picker@example.org", "long enough pw", virtual=False)
    token = csrf(client, "/accounts/connect/meross")
    login = {"email": "picker@example.org", "password": "x", "csrf_token": token}
    pick_url = client.post("/accounts/connect/meross", data=login, follow_redirects=False).headers[
        "location"
    ]

    page = client.get(pick_url).text
    assert '<a class="back-link" href="/settings#plugs">' in page  # "← Back", at the top
    assert "Living room lamp" not in page  # not a plug
    assert page.index("E-Bike") < page.index("Old plug")  # usable ones first
    old_card = page[page.index('<section class="card unsupported">') :]
    assert "Old plug" in old_card and "Can't measure power" in old_card
    assert "uuid-old" not in page  # no button for it
    assert 'value="uuid-bike"' in page and "None of your plugs" not in page

    r = client.post(
        pick_url,
        data={"vendor_device_id": "uuid-old", "default_hours": "3", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 400  # refused, also without the page

    devices[:] = [old, lamp]  # only an old plug: say why nothing can be picked
    page = client.get(pick_url + "?refresh=1").text
    assert "None of your plugs can measure power." in page and "MSS310" in page

    devices[:] = [old, bike, lamp]
    client.get(pick_url + "?refresh=1")  # the list is cached for a few minutes
    # picked: its settings; "← Back" leads to its dashboard there, not back to the picker
    r = client.post(
        pick_url,
        data={"vendor_device_id": "uuid-bike", "default_hours": "3", "csrf_token": token},
        follow_redirects=False,
    )
    dev = int(r.headers["location"].split("/")[2])
    page = client.get(r.headers["location"]).text
    assert f'<a class="back-link" href="/devices/{dev}">' in page
    later = client.get(f"/devices/{dev}/settings").text  # from the dashboard or the settings
    assert f'<a class="back-link" href="/devices/{dev}" data-back>' in later
