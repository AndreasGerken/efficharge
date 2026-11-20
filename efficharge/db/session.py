"""Engine and sessions for SQLite (WAL mode): write sessions and read sessions.

SQLite has one write lock per database. In WAL mode readers never block and are never
blocked, so only real writes should take it:

* **Write sessions** (``make_sessionmaker``) start with ``BEGIN IMMEDIATE``: they take the
  write lock at the start, so a second writer simply waits (``busy_timeout``). (A
  "deferred" transaction that first reads and later writes would instead fail at once
  with "database is locked" if someone else wrote in between.) Keep them short and never
  hold one across a slow await (vendor cloud calls, starting runners): ``commit()`` first.
* **Read sessions** (``readers_for``) start with a plain ``BEGIN`` and ``PRAGMA
  query_only``: no lock, a consistent snapshot, and an accidental write fails loudly.
  Use them for everything that only reads (GET pages, status, the supervisor's lookups).

The classic "database is locked" here was a request that held a write session (taken by
its first SELECT) while awaiting code that opened a second session: that one waited for
the lock its own request held. With reads in read sessions that cannot happen.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import time
import weakref

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

#: every SQLite connection (raw sqlite3) → where its last write transaction began.
#: Used only for the "waited for the write lock" log line below.
_began: dict[int, tuple[float, str, str]] = {}
_raw: dict[int, weakref.ref] = {}
SLOW_LOCK_S = 2.0


def _raw_sqlite(dbapi_conn):
    """The aiosqlite connection behind SQLAlchemy's adapter (or None). Its ``_conn`` is
    the sqlite3 connection, which can't be weak-referenced itself."""
    return getattr(dbapi_conn, "_connection", None)


def _where() -> str:
    """Our code that started the transaction: the running task's coroutine chain (the
    begin event itself runs in a greenlet without our frames on its stack)."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    frames = []
    if task is not None:
        coro = task.get_coro()
        while coro is not None:
            frame = getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
            if frame is not None and "/efficharge/" in frame.f_code.co_filename:
                name = frame.f_code.co_filename.rsplit("/efficharge/", 1)[1]
                frames.append(f"{name}:{frame.f_lineno}")
            coro = getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None)
    test = os.environ.get("PYTEST_CURRENT_TEST", "")
    return " > ".join(frames[-4:]) + (f" [{test}]" if test else "")


def _task_name() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return "?"
    return task.get_name() if task else "?"


def _lock_holders(me) -> str:
    """Connections that are inside a transaction right now (SQLite's own flag)."""
    now = time.monotonic()
    out = []
    for key, ref in list(_raw.items()):
        raw = ref()
        if raw is None:
            _raw.pop(key, None)
            continue
        sqlite = getattr(raw, "_conn", None)
        if raw is me or sqlite is None or not sqlite.in_transaction:
            continue
        since, task, where = _began.get(key, (now, "?", "?"))
        out.append(f"{task} since {now - since:.1f}s, began at {where}")
    return "; ".join(out) or "none in this process"


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            # we issue BEGIN ourselves (see below)
            dbapi_conn.isolation_level = None
            raw = _raw_sqlite(dbapi_conn)
            if raw is not None:
                _raw[id(raw)] = weakref.ref(raw)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        @event.listens_for(engine.sync_engine, "begin")
        def _begin(conn):  # pragma: no cover - trivial
            # connections are pooled and shared by both kinds: set the mode every time
            if conn.get_execution_options().get("read_only"):
                conn.exec_driver_sql("PRAGMA query_only=1")
                conn.exec_driver_sql("BEGIN")
            else:
                conn.exec_driver_sql("PRAGMA query_only=0")
                raw = _raw_sqlite(conn.connection.dbapi_connection)
                started = time.monotonic()
                try:
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                finally:
                    waited = time.monotonic() - started
                    if waited > SLOW_LOCK_S:
                        logger.warning(
                            "waited %.1fs for the write lock at %s; in a transaction: %s",
                            waited,
                            _where(),
                            _lock_holders(raw),
                        )
                if raw is not None:
                    _began[id(raw)] = (time.monotonic(), _task_name(), _where())

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    """Write sessions."""
    return async_sessionmaker(engine, expire_on_commit=False)


def readers_for(sessions: async_sessionmaker) -> async_sessionmaker:
    """Read-only sessions on the same database as the write ``sessions``."""
    readers = getattr(sessions, "_efficharge_readers", None)
    if readers is None:
        engine: AsyncEngine = sessions.kw["bind"]
        readers = async_sessionmaker(
            engine.execution_options(read_only=True), expire_on_commit=False
        )
        sessions._efficharge_readers = readers
    return readers


def cancel_safe(fn):
    """For write methods of background tasks (runners, fetcher, demo).

    Cancelling a task while a statement runs in aiosqlite's thread can leave its
    transaction open, holding the write lock until the connection is cleaned up: every
    other writer then fails with "database is locked". Runners are cancelled whenever a
    plug is stopped or restarted (settings saved, reconnect, shutdown). So the write
    runs in its own task that always finishes (commit or rollback); the caller is still
    cancelled, right after."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        task = asyncio.ensure_future(fn(*args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
            raise

    return wrapper
