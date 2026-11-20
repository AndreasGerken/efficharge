"""The beta before launch: sign-ups open, no start page, no demo; an admin accepts them."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from efficharge.config import Config
from efficharge.crypto import generate_key
from efficharge.db.migrate import upgrade
from efficharge.db.models import Device, User
from efficharge.db.session import make_engine, make_sessionmaker
from efficharge.users_cli import add_user
from efficharge.web.app import create_app
from efficharge.web.auth import reset_limiters

ADMIN = "boss@example.org"


@pytest.fixture
def beta(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        registration_open=True,
        demo=False,
        about_page=False,
        admins=frozenset({ADMIN}),
        support_email="help@example.org",
    )
    reset_limiters()
    app = create_app(cfg, start_workers=False)
    with TestClient(app, headers={"accept-language": "en"}, follow_redirects=False) as c:
        yield c, url


def token(c: TestClient, path: str) -> str:
    return re.search(r'"X-CSRF-Token": "([^"]+)"', c.get(path).text).group(1)


def sign_up(c: TestClient, email: str, about: str = "a neighbour") -> None:
    data = {"email": email, "password": "long enough pw", "about": about}
    r = c.post("/register", data={**data, "csrf_token": token(c, "/register")})
    assert "checked by hand" in r.text


def log_in(c: TestClient, email: str, password: str = "long enough pw") -> int:
    c.cookies.clear()
    data = {"email": email, "password": password, "csrf_token": token(c, "/login")}
    return c.post("/login", data=data).status_code


def make_admin(c: TestClient, url: str) -> None:
    asyncio.run(add_user(url, ADMIN, "admin password", timezone="Europe/Berlin", locale="en"))
    assert log_in(c, ADMIN, "admin password") == 303


def db_state(url: str, email: str) -> tuple[User | None, int]:
    async def go():
        engine = make_engine(url)
        try:
            async with make_sessionmaker(engine)() as db:
                user = await db.scalar(select(User).where(User.email == email))
                plugs = 0
                if user is not None:
                    plugs = len(
                        (await db.scalars(select(Device).where(Device.user_id == user.id))).all()
                    )
                return user, plugs
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_without_the_about_page_only_login_and_sign_up_are_public(beta):
    c, _ = beta
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/about").status_code == 404
    assert c.get("/demo").status_code == 404
    login = c.get("/login").text
    assert 'href="/about"' not in login and "/demo" not in login
    assert 'href="/register"' in login  # the way in
    register = c.get("/register")
    assert register.status_code == 200
    # use at your own risk: a real link to the library, the battery is safe, whom to write
    assert (
        '<a href="https://github.com/albertogeniola/MerossIot" rel="noopener">meross_iot</a>'
        in register.text
    )
    assert "Your battery is not at risk" in register.text
    assert '<a href="mailto:help@example.org">help@example.org</a>' in register.text


def test_admin_accepts_and_rejects_registrations(beta):
    c, url = beta
    sign_up(c, "new@example.org", "Erika, from the bike club")
    sign_up(c, "spam@example.org")
    assert log_in(c, "new@example.org") == 403  # not accepted yet

    make_admin(c, url)
    assert "2 registrations waiting" in c.get("/settings").text
    page = c.get("/admin").text
    assert '<a class="back-link" href="/settings">' in page
    assert '<a class="back-link" href="/settings#plugs">' in c.get("/accounts/connect/meross").text
    assert "new@example.org" in page and "Erika, from the bike club" in page
    assert ADMIN in page  # the accounts list: the admin themself
    new, plugs = db_state(url, "new@example.org")
    assert plugs == 0

    # without the CSRF token: nothing happens
    assert c.post(f"/admin/users/{new.id}/approve").status_code == 403
    csrf = {"csrf_token": token(c, "/admin")}
    r = c.post(f"/admin/users/{new.id}/approve", data=csrf)
    assert r.headers["location"] == "/admin?done=approved"
    assert "they can log in now" in c.get("/admin?done=approved").text
    assert c.post(f"/admin/users/{new.id}/approve", data=csrf).status_code == 404  # done
    assert db_state(url, "new@example.org")[1] == 0  # no plug yet: Settings welcomes them

    spam, _ = db_state(url, "spam@example.org")
    r = c.post(  # the button asks first (hx-confirm), then htmx follows HX-Redirect
        f"/admin/users/{spam.id}/reject",
        headers={"HX-Request": "true", "X-CSRF-Token": csrf["csrf_token"]},
    )
    assert r.headers["hx-redirect"] == "/admin?done=rejected"
    assert db_state(url, "spam@example.org")[0] is None
    assert "no registrations waiting" in c.get("/settings").text
    assert c.post(f"/admin/users/{new.id}/reject", data=csrf).status_code == 404  # accepted

    assert log_in(c, "new@example.org") == 303
    assert c.get("/devices").headers["location"] == "/settings"
    assert "Welcome to efficharge!" in c.get("/settings").text


def test_admin_page_is_only_for_admins(beta):
    c, url = beta
    sign_up(c, "someone@example.org")
    sign_up(c, "other@example.org")
    make_admin(c, url)
    other, _ = db_state(url, "other@example.org")
    c.post(f"/admin/users/{other.id}/approve", data={"csrf_token": token(c, "/admin")})
    someone, _ = db_state(url, "someone@example.org")

    assert log_in(c, "other@example.org") == 303  # accepted, but not an admin
    assert c.get("/admin").status_code == 404
    assert 'href="/admin"' not in c.get("/settings").text
    r = c.post(f"/admin/users/{someone.id}/approve", data={"csrf_token": token(c, "/settings")})
    assert r.status_code == 404
    assert db_state(url, "someone@example.org")[0].approved_at is None

    c.cookies.clear()
    assert c.get("/admin").headers["location"].startswith("/login")
