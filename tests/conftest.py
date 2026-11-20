from __future__ import annotations

import asyncio

import pytest

from efficharge.crypto import Vault, generate_key
from efficharge.db.migrate import upgrade
from efficharge.db.session import make_engine, make_sessionmaker


@pytest.fixture
async def db(tmp_path):
    """A fresh SQLite DB created by the real migrations (not create_all)."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await asyncio.to_thread(upgrade, url)
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def vault() -> Vault:
    return Vault.from_config(f"k1:{generate_key()}")
