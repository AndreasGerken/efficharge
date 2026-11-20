"""Ways to abuse the site, and that each is closed."""

from __future__ import annotations

from sqlalchemy import func, select

from efficharge.db.models import DeadlineRuleRow, User
from efficharge.web import app as web_app
from efficharge.web import auth
from tests.test_web import add_virtual_plug, csrf, register

PW = "long enough pw"


def log_in(c, email: str, password: str = PW):
    data = {"email": email, "password": password, "csrf_token": csrf(c, "/login")}
    return c.post("/login", data=data, follow_redirects=False)


def users(c) -> int:
    async def count():
        async with c.app.state.rt.sessions() as s:
            return await s.scalar(select(func.count()).select_from(User))

    return c.portal.call(count)


def test_sign_up_does_not_tell_which_emails_have_an_account(client):
    register(client, "taken@example.org", PW, virtual=False)
    client.cookies.clear()
    before = users(client)
    data = {"email": "Taken@Example.org", "password": "another password", "about": "me"}
    r = client.post("/register", data={**data, "csrf_token": csrf(client, "/register")})
    assert r.status_code == 200 and "checked by hand" in r.text  # as for a new address
    assert "already" not in r.text
    assert users(client) == before  # nothing stored, the old password still works
    assert log_in(client, "taken@example.org").status_code == 303

    # and it counts against the limit per address, so nobody can test addresses in bulk
    client.cookies.clear()
    for n in range(2):
        data = {"email": f"probe{n}@example.org", "password": PW, "about": "x"}
        client.post("/register", data={**data, "csrf_token": csrf(client, "/register")})
    data = {"email": "taken@example.org", "password": PW, "about": "x"}
    r = client.post("/register", data={**data, "csrf_token": csrf(client, "/register")})
    assert r.status_code == 400 and "Too many registrations" in r.text


def test_wrong_passwords_per_account_from_many_addresses(client, monkeypatch):
    register(client, "victim@example.org", PW, virtual=False)
    client.cookies.clear()
    monkeypatch.setattr(auth.account_limiter, "limit", 3)
    for _ in range(3):  # three guesses (in real life from three machines)
        assert log_in(client, "victim@example.org", "guess guess").status_code == 400
    r = log_in(client, "victim@example.org")  # now even the right one waits
    assert r.status_code == 429 and "Too many wrong passwords" in r.text
    assert log_in(client, "other@example.org", "guess guess").status_code == 400  # others not
    # an unknown email behaves the same: the limit doesn't reveal which accounts exist
    for _ in range(3):
        log_in(client, "nobody@example.org", "guess guess")
    assert log_in(client, "nobody@example.org", "guess guess").status_code == 429


def test_a_login_flood_cannot_stall_the_app(client, monkeypatch):
    monkeypatch.setattr(auth.password_budget, "limit", 2)
    assert log_in(client, "a@example.org", "wrong one!").status_code == 400
    assert log_in(client, "b@example.org", "wrong one!").status_code == 400
    r = log_in(client, "c@example.org", "wrong one!")  # the third check this minute
    assert r.status_code == 429 and "Too many logins right now" in r.text


def test_overlong_passwords_are_not_hashed(client, monkeypatch):
    monkeypatch.setattr(auth.password_budget, "limit", 1)
    huge = "x" * (auth.MAX_PASSWORD + 1)
    for _ in range(3):  # wrong, and no hashing time spent (the budget of 1 stays)
        assert log_in(client, "a@example.org", huge).status_code == 400
    data = {"email": "new@example.org", "password": huge, "about": "me"}
    r = client.post("/register", data={**data, "csrf_token": csrf(client, "/register")})
    assert r.status_code == 400 and "at most 1000 characters" in r.text


def test_refresh_reads_the_plug_at_most_every_10_s(client, monkeypatch):
    register(client, "fresh@example.org", PW, virtual=False)
    dev = add_virtual_plug(client)
    client.get(f"/devices/{dev}")  # opens the virtual plug: its runner starts
    reads = []

    async def fake_refresh(request, device_id, timeout_s=5.0):
        reads.append(device_id)

    monkeypatch.setattr(web_app, "_refresh", fake_refresh)
    token = csrf(client, f"/devices/{dev}")
    for _ in range(3):
        r = client.post(
            f"/devices/{dev}/command", data={"action": "refresh"}, headers={"X-CSRF-Token": token}
        )
        assert r.status_code == 200
    assert reads == [dev]  # one read of the cloud, the other two got the stored status


def test_ready_by_must_be_a_date_within_two_weeks(client):
    register(client, "ready@example.org", PW, virtual=False)
    dev = add_virtual_plug(client)
    client.get(f"/devices/{dev}")
    token = csrf(client, f"/devices/{dev}")

    def ready_by(value: str) -> int:
        data = {"action": "ready_by", "ready_by": value}
        return client.post(
            f"/devices/{dev}/command", data=data, headers={"X-CSRF-Token": token}
        ).status_code

    assert ready_by("not a date") == 400  # was an error 500
    assert ready_by("2999-01-01T07:30") == 400


def test_at_most_20_rules_per_plug(client, monkeypatch):
    register(client, "rules@example.org", PW, virtual=False)
    dev = add_virtual_plug(client)
    monkeypatch.setattr(web_app, "MAX_RULES", 2)
    token = csrf(client, f"/devices/{dev}/settings")

    def add_rule() -> int:
        return client.post(
            f"/devices/{dev}/rules", data={"csrf_token": token}, follow_redirects=False
        ).status_code

    async def rules() -> int:
        async with client.app.state.rt.sessions() as s:
            q = select(func.count()).select_from(DeadlineRuleRow)
            return await s.scalar(q.where(DeadlineRuleRow.device_id == dev))

    while client.portal.call(rules) < 2:
        assert add_rule() == 303
    assert add_rule() == 400


def test_browser_hardening_headers(client):
    r = client.get("/login")
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cross-origin-opener-policy"] == "same-origin"


def test_limiter_forgets_empty_and_idle_keys():
    limiter = auth.RateLimiter(limit=1, window_s=60)
    for n in range(100):
        limiter.full(f"probe{n}")  # checking alone leaves nothing behind
    limiter.hit("real")
    assert list(limiter._hits) == ["real"]
