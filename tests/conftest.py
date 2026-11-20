from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from efficharge.config import Config
from efficharge.crypto import Vault, generate_key
from efficharge.db.migrate import upgrade
from efficharge.db.session import make_engine, make_sessionmaker
from efficharge.energy.client import BASE_URL, EnergyChartsClient
from efficharge.web.app import create_app
from efficharge.web.auth import reset_limiters


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


def energy_api() -> EnergyChartsClient:
    """48 h of fake data around now: expensive now, cheap in 5 h."""
    start = int(datetime.now(UTC).replace(minute=0, second=0, microsecond=0).timestamp()) - 3600
    ts = [start + 900 * i for i in range(48 * 4)]
    price = [300.0 if not 20 <= i < 36 else 20.0 for i in range(len(ts))]
    ren = [30.0 if not 20 <= i < 36 else 80.0 for i in range(len(ts))]

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "/price": {"unix_seconds": ts, "price": price},
            "/ren_share_forecast": {"unix_seconds": ts, "ren_share": ren},
            "/co2eq": {"unix_seconds": ts, "co2eq": [400.0] * len(ts)},
            "/public_power": {"unix_seconds": ts, "production_types": []},
        }[request.url.path]
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return EnergyChartsClient(http=http, backoff=0)


@pytest.fixture
def client(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'web.db'}"
    upgrade(url)
    cfg = Config(
        database_url=url,
        data_keys=f"k1:{generate_key()}",
        secret_key="test-secret-" + "x" * 40,
        cookie_secure=False,
        registration_open=True,
        demo=True,
        poll_interval=timedelta(milliseconds=50),
    )
    reset_limiters()
    app = create_app(cfg, energy_client=energy_api())
    with TestClient(app, headers={"accept-language": "en"}) as c:
        # the first energy fetch runs in the background: tests expect its prices
        deadline = time.monotonic() + 5
        while app.state.rt.fetcher.last_success is None:
            assert time.monotonic() < deadline, "no energy data after 5 s"
            time.sleep(0.01)
        yield c
