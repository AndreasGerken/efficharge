"""Passwords (argon2id), sessions, CSRF and a login rate limit."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
#: verify against this when the email is unknown, so timing doesn't reveal accounts
_DUMMY_HASH = _hasher.hash("not-a-real-password")

MIN_PASSWORD = 10
#: longer is no password anyone types: it would only cost hashing time
MAX_PASSWORD = 1000


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)


# ── session ──


def _stamp(user) -> str:
    """Changes with the password: a new password logs out every other session. The email
    is part of it, so a session of a deleted user never fits a new user who gets the same
    id (SQLite reuses ids; demo users, who have no password, are deleted often)."""
    raw = f"{user.id}|{user.email}|{user.password_hash or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def login_session(request: Request, user, locale: str) -> None:
    request.session.clear()  # new session on login (no fixation)
    request.session["uid"] = user.id
    request.session["pw"] = _stamp(user)
    request.session["locale"] = locale
    request.session["csrf"] = secrets.token_urlsafe(32)


def session_valid(request: Request, user) -> bool:
    return hmac.compare_digest(request.session.get("pw", ""), _stamp(user))


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


async def check_csrf(request: Request) -> None:
    """For every state-changing request: header (HTMX) or form field (plain forms)."""
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        sent = form.get("csrf_token")
    if not expected or not isinstance(sent, str) or not hmac.compare_digest(expected, sent):
        raise HTTPException(status_code=403, detail="CSRF check failed")


# ── rate limit ──


class RateLimiter:
    """In-memory sliding window. Fine for one process (which we have, by design)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """Record an attempt. False = over the limit."""
        hits = self._recent(key)
        if len(hits) >= self.limit:
            return False
        hits.append(time.monotonic())
        return True

    def full(self, key: str) -> bool:
        """Over the limit, without recording an attempt."""
        return len(self._recent(key)) >= self.limit

    def _recent(self, key: str) -> deque[float]:
        now = time.monotonic()
        for k in [k for k, h in self._hits.items() if not h or h[-1] < now - self.window_s]:
            del self._hits[k]  # forget idle and empty keys: the dict can't grow without bound
        hits = self._hits[key]
        while hits and hits[0] < now - self.window_s:
            hits.popleft()
        return hits


login_limiter = RateLimiter(limit=10, window_s=15 * 60)  # per IP + email
ip_limiter = RateLimiter(limit=30, window_s=15 * 60)  # per IP, whatever the email
#: wrong passwords per account, from all addresses together (guessing from many machines)
account_limiter = RateLimiter(limit=20, window_s=60 * 60)
#: against bots: new public demos and registrations per IP and hour
demo_limiter = RateLimiter(limit=3, window_s=60 * 60)
register_limiter = RateLimiter(limit=3, window_s=60 * 60)
#: Every password check or new hash costs ~60 ms of CPU and 64 MB, in the process that
#: also switches the plugs. At most this many per minute overall: a flood from many
#: addresses gets "try again" instead of stalling the app.
password_budget = RateLimiter(limit=60, window_s=60)

LIMITERS = (
    login_limiter,
    ip_limiter,
    account_limiter,
    demo_limiter,
    register_limiter,
    password_budget,
)


def reset_limiters() -> None:
    """Tests: every limiter empty."""
    for limiter in LIMITERS:
        limiter._hits.clear()


class Busy(Exception):
    """Too many password checks right now: answer "try again" without hashing."""


def check_password(stored: str | None, password: str) -> bool:
    """``verify_password`` within the overall budget (raises ``Busy``); an overlong
    password is simply wrong, without hashing it."""
    if len(password) > MAX_PASSWORD:
        return False
    if not password_budget.hit("all"):
        raise Busy
    return verify_password(stored, password)


def new_password_hash(password: str) -> str:
    """``hash_password`` within the overall budget (raises ``Busy``)."""
    if not password_budget.hit("all"):
        raise Busy
    return hash_password(password)
