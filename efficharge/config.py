"""Configuration from environment variables (``EFFICHARGE_*``). Secrets never live in the repo.

On the server they come from ``/etc/efficharge/efficharge.env`` (mode 0600), loaded by
systemd's ``EnvironmentFile=``. Locally, export them or use a ``.env`` with your shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(f"EFFICHARGE_{name}", default)
    if value is None:
        raise RuntimeError(f"environment variable EFFICHARGE_{name} is required")
    return value


def _flag(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    database_url: str
    #: "kid:base64key,kid:base64key" (first = current), see efficharge.crypto
    data_keys: str
    #: signs the session cookie; long random string
    secret_key: str
    bidding_zone: str = "DE-LU"
    country: str = "de"
    default_timezone: str = "Europe/Berlin"
    fetch_interval: timedelta = timedelta(minutes=15)
    poll_interval: timedelta = timedelta(seconds=30)
    #: False only for local http development
    cookie_secure: bool = True
    registration_open: bool = False
    #: create and run the public demo (simulated plug)
    demo: bool = True
    #: the public start page (/about, and / without a login). Off: / leads to /login
    about_page: bool = True
    #: login emails that see /admin (EFFICHARGE_ADMINS, comma-separated)
    admins: frozenset[str] = frozenset()
    #: Impressum (§ 5 DDG): kept in the server's env file, not in the code
    imprint_name: str = ""
    imprint_address: str = ""  # one line, e.g. "Musterstraße 1, 12345 Berlin"
    imprint_email: str = ""
    #: where users write when something doesn't work (empty: the imprint email)
    support_email: str = ""
    #: optional "Buy me a coffee" link in the footer and on the start page
    coffee_url: str = ""
    #: AGPL § 13: users of the site get a link to its source. Point it to your fork.
    source_url: str = "https://github.com/AndreasGerken/efficharge"
    #: let search engines index the site (off until launch)
    allow_indexing: bool = False
    #: privacy notice: who runs the server for you (name, address), if anyone
    hosting_provider: str = ""
    #: privacy notice: the oldest database backup, in days (all copies, wherever they are)
    backup_days: int = 30

    def is_admin(self, user) -> bool:
        return user is not None and not user.is_demo and user.email in self.admins

    @property
    def contact_email(self) -> str:
        """Where users write when something doesn't work."""
        return self.support_email or self.imprint_email

    @property
    def has_imprint(self) -> bool:
        return bool(self.imprint_name and self.imprint_address and self.imprint_email)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            database_url=_env("DATABASE_URL", "sqlite+aiosqlite:///efficharge.db"),
            data_keys=_env("DATA_KEYS"),
            secret_key=_env("SECRET_KEY"),
            bidding_zone=_env("BIDDING_ZONE", "DE-LU"),
            country=_env("COUNTRY", "de"),
            default_timezone=_env("TIMEZONE", "Europe/Berlin"),
            fetch_interval=timedelta(seconds=int(_env("FETCH_INTERVAL_S", "900"))),
            poll_interval=timedelta(seconds=int(_env("POLL_INTERVAL_S", "30"))),
            cookie_secure=_flag("COOKIE_SECURE", True),
            registration_open=_flag("REGISTRATION_OPEN", False),
            demo=_flag("DEMO", True),
            about_page=_flag("ABOUT_PAGE", True),
            admins=frozenset(e.strip().lower() for e in _env("ADMINS", "").split(",") if e.strip()),
            imprint_name=_env("IMPRINT_NAME", ""),
            imprint_address=_env("IMPRINT_ADDRESS", ""),
            imprint_email=_env("IMPRINT_EMAIL", ""),
            support_email=_env("SUPPORT_EMAIL", ""),
            coffee_url=_env("COFFEE_URL", ""),
            source_url=_env("SOURCE_URL", "https://github.com/AndreasGerken/efficharge"),
            allow_indexing=_flag("ALLOW_INDEXING", False),
            hosting_provider=_env("HOSTING_PROVIDER", ""),
            backup_days=int(_env("BACKUP_DAYS", "30")),
        )
