"""Read sessions never wait for the write lock, and they can't write."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from efficharge.db.models import User
from efficharge.db.repo import create_user
from efficharge.db.session import readers_for


async def test_reading_while_a_write_session_holds_the_lock(db):
    async with db() as s, s.begin():
        await create_user(s, "a@example.org")
    async with db() as writer:
        # the request pattern that used to fail: a write session took the lock with its
        # first SELECT, then code awaited inside the same request opens another session
        await writer.scalar(select(User.id))
        started = time.monotonic()
        async with readers_for(db)() as reader:
            assert await reader.scalar(select(User.email)) == "a@example.org"
        assert time.monotonic() - started < 1.0  # no waiting for the lock
        await writer.commit()


async def test_read_sessions_cannot_write(db):
    async with readers_for(db)() as reader:
        with pytest.raises(OperationalError, match="readonly database"):
            await create_user(reader, "b@example.org")  # flushes an INSERT
    # the pooled connection is a normal write connection again afterwards
    async with db() as s, s.begin():
        await create_user(s, "c@example.org")


async def test_writers_queue_instead_of_failing(db):
    async def write(n: int):
        async with db() as s, s.begin():
            await create_user(s, f"u{n}@example.org")
            await asyncio.sleep(0.05)  # hold the lock a moment

    await asyncio.gather(*(write(n) for n in range(5)))
    async with readers_for(db)() as reader:
        assert len((await reader.scalars(select(User.id))).all()) == 5
