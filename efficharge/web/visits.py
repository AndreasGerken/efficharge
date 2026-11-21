"""Anonymous visit counts: page views and visitors per day, for the admin page.

Nothing about a visitor is stored. To count a visitor once per day, the counter keeps a
hash of IP address and browser with a random key in memory; the key and the hashes are
thrown away when the day changes (and on every restart, which may count someone twice).
Only the numbers reach the database. Bots and the admins' own visits are not counted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.sqlite import insert

from efficharge.db.models import VisitCount

logger = logging.getLogger(__name__)

TOTAL = "*"  # the row with all pages of a day
DEMO_STARTS = "demo started"
FLUSH_EVERY_S = 60
MAX_SEEN = 100_000  # hashes per day; above that a visitor may be counted twice
BOTS = re.compile(
    r"bot|crawl|spider|slurp|scrap|curl|wget|python|httpx|aiohttp|go-http|java/|okhttp|"
    r"headless|lighthouse|monitor|uptime|preview|facebookexternalhit|scan|fetch",
    re.IGNORECASE,
)


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(BOTS.search(user_agent))


class VisitCounter:
    def __init__(self, timezone: str) -> None:
        self.tz = ZoneInfo(timezone)
        self.day = ""
        self.key = b""
        self.seen: set[tuple[bytes, str]] = set()
        self.views: Counter[tuple[str, str]] = Counter()  # (day, page) → not yet stored
        self.visitors: Counter[tuple[str, str]] = Counter()

    def _today(self, now: datetime) -> str:
        day = now.astimezone(self.tz).date().isoformat()
        if day != self.day:  # a new day: forget who was here
            self.day, self.key, self.seen = day, secrets.token_bytes(16), set()
        return day

    def page_view(self, now: datetime, page: str, ip: str, user_agent: str) -> None:
        if is_bot(user_agent):
            return
        day = self._today(now)
        who = hashlib.blake2b(f"{ip}\n{user_agent}".encode(), key=self.key, digest_size=8)
        for p in (TOTAL, page):
            self.views[day, p] += 1
            seen = (who.digest(), p)
            if seen not in self.seen:
                if len(self.seen) < MAX_SEEN:
                    self.seen.add(seen)
                self.visitors[day, p] += 1

    def event(self, now: datetime, name: str) -> None:
        self.views[self._today(now), name] += 1

    async def flush(self, sessions) -> None:
        """Add the counts since the last flush to the database."""
        if not self.views:
            return
        views, visitors = self.views, self.visitors
        self.views, self.visitors = Counter(), Counter()
        rows = [
            {"day": d, "page": p, "views": n, "visitors": visitors[d, p]}
            for (d, p), n in views.items()
        ]
        q = insert(VisitCount).values(rows)
        q = q.on_conflict_do_update(
            index_elements=["day", "page"],
            set_={
                "views": VisitCount.views + q.excluded.views,
                "visitors": VisitCount.visitors + q.excluded.visitors,
            },
        )
        try:
            async with sessions() as s:
                await s.execute(q)
                await s.commit()
        except Exception:
            self.views.update(views)  # try again next time
            self.visitors.update(visitors)
            raise

    async def run(self, sessions) -> None:
        try:
            while True:
                await asyncio.sleep(FLUSH_EVERY_S)
                try:
                    await self.flush(sessions)
                except Exception:
                    logger.exception("storing the visit counts failed")
        finally:
            with contextlib.suppress(Exception):
                await self.flush(sessions)  # on shutdown: nothing gets lost
