"""A mail to the admins when someone signs up, so registrations don't wait unseen.

At most one mail per hour, which then counts everyone waiting ("3 sign-ups are waiting").
It carries no personal data: who signed up is only visible at /admin. A failed mail is
logged and tried again later; the registration itself never waits for it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from efficharge.db.models import User
from efficharge.db.session import readers_for
from efficharge.mail import Mailer

logger = logging.getLogger(__name__)

EVERY = timedelta(hours=1)
RETRY_AFTER = timedelta(minutes=10)
CHECK_S = 60.0  # while the hour runs: look at the clock this often


class SignupNotifier:
    def __init__(
        self, mailer: Mailer, to: list[str], sessions: async_sessionmaker, clock, admin_url: str
    ) -> None:
        self.mailer, self.to, self.clock, self.admin_url = mailer, to, clock, admin_url
        self.readers = readers_for(sessions)
        self.wake = asyncio.Event()
        self.last_sent: datetime | None = None

    def new_signup(self) -> None:
        self.wake.set()

    async def pending(self) -> int:
        async with self.readers() as s:
            q = select(func.count()).select_from(User)
            return await s.scalar(q.where(User.approved_at.is_(None), User.is_demo.is_(False)))

    async def notify(self) -> None:
        """One mail about everyone waiting now (none if nobody is)."""
        n = await self.pending()
        if not n:
            return
        what = "1 sign-up is" if n == 1 else f"{n} sign-ups are"
        body = (
            f"{what} waiting for you at efficharge.\n\n"
            f"Accept or reject at {self.admin_url}\n\n"
            "(At most one of these mails per hour.)\n"
        )
        await self.mailer.send(self.to, f"efficharge: {what} waiting", body)
        self.last_sent = self.clock()
        logger.info("sign-up mail sent (%d waiting)", n)

    async def run(self) -> None:
        while True:
            await self.wake.wait()
            while self.last_sent is not None and self.clock() < self.last_sent + EVERY:
                await asyncio.sleep(CHECK_S)
            self.wake.clear()  # sign-ups from now on get the next mail
            try:
                await self.notify()
            except Exception:
                logger.exception("the sign-up mail failed")
                self.wake.set()
                await asyncio.sleep(RETRY_AFTER.total_seconds())
