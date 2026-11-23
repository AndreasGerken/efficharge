"""The mail to the admins about new sign-ups: hourly at most, no personal data, never in the way."""

from __future__ import annotations

import smtplib
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from efficharge.config import Config
from efficharge.crypto import generate_key
from efficharge.db.migrate import upgrade
from efficharge.mail import Mailer
from efficharge.web import signups
from efficharge.web.app import create_app
from efficharge.web.auth import reset_limiters
from tests.test_admin import ADMIN, sign_up, token


@pytest.fixture
def sent(monkeypatch):
    """The mails the app sends (nothing leaves the machine)."""
    mails = []
    monkeypatch.setattr(Mailer, "_send", lambda self, msg: mails.append(msg))
    monkeypatch.setattr(signups, "RETRY_AFTER", timedelta(seconds=0.1))
    monkeypatch.setattr(signups, "CHECK_S", 0.01)
    return mails


def config(tmp_path, **mail) -> Config:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    return Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        registration_open=True,
        demo=False,
        about_page=False,
        admins=frozenset({ADMIN}),
        public_url="https://efficharge.example/",
        **mail,
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mailing(tmp_path, clock):
    cfg = config(tmp_path, smtp_host="smtp.example", smtp_user="noreply@efficharge.example")
    reset_limiters()
    app = create_app(cfg, start_workers=False, clock=clock)
    with TestClient(app, headers={"accept-language": "en"}) as c:
        yield c


def wait_for(mails: list, n: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while len(mails) < n:
        assert time.monotonic() < deadline, f"{len(mails)} mails, expected {n}"
        time.sleep(0.02)


def test_a_sign_up_sends_one_mail_without_personal_data(mailing, sent):
    sign_up(mailing, "new.person@example.org", about="Erika from next door")
    wait_for(sent, 1)
    msg = sent[0]
    assert msg["To"] == ADMIN and msg["From"] == "noreply@efficharge.example"
    assert msg["Subject"] == "efficharge: 1 sign-up is waiting"
    assert msg["Message-ID"].endswith("@efficharge.example>")
    body = msg.get_content()
    assert "https://efficharge.example/admin" in body
    assert "new.person" not in body and "Erika" not in body


def test_at_most_one_mail_per_hour_counting_everyone_waiting(mailing, sent, clock):
    sign_up(mailing, "a@example.org")
    wait_for(sent, 1)
    sign_up(mailing, "b@example.org")
    clock.now += timedelta(minutes=30)
    sign_up(mailing, "c@example.org")
    time.sleep(0.3)
    assert len(sent) == 1  # the hour isn't over
    clock.now += timedelta(minutes=31)
    wait_for(sent, 2)
    assert sent[1]["Subject"] == "efficharge: 3 sign-ups are waiting"
    clock.now += timedelta(hours=2)
    time.sleep(0.3)
    assert len(sent) == 2  # nobody new: no mail


def test_known_emails_and_bots_send_nothing(mailing, sent, clock):
    sign_up(mailing, "a@example.org")
    wait_for(sent, 1)
    clock.now += timedelta(hours=2)
    sign_up(mailing, "A@example.org")  # has an account: the same answer, nothing stored
    data = {"email": "bot@example.org", "password": "long enough pw", "about": "x", "website": "x"}
    mailing.post("/register", data={**data, "csrf_token": token(mailing, "/register")})
    time.sleep(0.3)
    assert len(sent) == 1


def test_a_failing_mail_server_never_stops_a_sign_up(mailing, sent, monkeypatch):
    def down(self, msg):
        monkeypatch.setattr(Mailer, "_send", lambda self, msg: sent.append(msg))  # back up
        raise smtplib.SMTPServerDisconnected("down")

    monkeypatch.setattr(Mailer, "_send", down)
    sign_up(mailing, "a@example.org")  # asserts the normal "checked by hand" answer
    wait_for(sent, 1)  # tried again


def test_no_mail_without_a_mail_server(tmp_path):
    assert create_app(config(tmp_path), start_workers=False).state.rt.signups is None


def test_mail_goes_only_encrypted(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout, context=None):
            calls.append(("connect", type(self).__name__, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def starttls(self, context):
            calls.append("starttls")

        def login(self, user, password):
            calls.append("login")

        def send_message(self, msg):
            calls.append("send")

    class SMTP_SSL(FakeSMTP):
        pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", SMTP_SSL)
    mailer = Mailer("smtp.example", 587, "me@example.org", "secret", "me@example.org")
    mailer._send(mailer.message(["you@example.org"], "hi", "text"))
    assert calls == [("connect", "FakeSMTP", 587), "starttls", "login", "send"]
    calls.clear()
    mailer.port = 465
    mailer._send(mailer.message(["you@example.org"], "hi", "text"))
    assert calls == [("connect", "SMTP_SSL", 465), "login", "send"]


def test_the_password_stays_out_of_logs():
    cfg = Config(database_url="x", data_keys="x", secret_key="x", smtp_password="hunter2")
    assert "hunter2" not in repr(cfg)


def test_sign_up_mails_go_to_the_admins_unless_set():
    cfg = Config(database_url="x", data_keys="x", secret_key="x", admins=frozenset({"a@x.org"}))
    assert cfg.signup_mail_to == ["a@x.org"]
    cfg = Config(
        database_url="x", data_keys="x", secret_key="x", notify_emails=frozenset({"n@x.org"})
    )
    assert cfg.signup_mail_to == ["n@x.org"]


def test_test_mail_command(monkeypatch, sent, capsys):
    from efficharge import cli

    for name, value in {
        "DATA_KEYS": "x",
        "SECRET_KEY": "x",
        "ADMINS": "Admin@Example.org",
        "SMTP_HOST": "smtp.example",
        "SMTP_USER": "noreply@example.org",
    }.items():
        monkeypatch.setenv(f"EFFICHARGE_{name}", value)
    cli.main(["test-mail"])
    assert sent[0]["To"] == "admin@example.org" and sent[0]["Subject"] == "efficharge: test mail"
    assert "sent from noreply@example.org" in capsys.readouterr().out
