"""Alembic environment (async). Batch mode, because SQLite can't ALTER most things."""

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from efficharge.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("url") or os.environ.get(
        "EFFICHARGE_DATABASE_URL", "sqlite+aiosqlite:///efficharge.db"
    )


def _configure(connection=None, url=None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )


def run_offline() -> None:
    _configure(url=_url())
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from code with an open connection
        _run_sync(connection)
        return
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run_sync)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
