import os

import httpx
import pytest

from efficharge.energy.client import BASE_URL, EnergyChartsClient, EnergyDataError
from efficharge.energy.series import EnergySeries
from tests.helpers import T0, at

TS = [int(T0.timestamp()), int(at(minutes=15).timestamp())]

RESPONSES = {
    "/price": {"unix_seconds": TS, "price": [100.0, 80.5], "deprecated": False},
    "/ren_share_forecast": {"unix_seconds": TS, "ren_share": [40.0, None], "deprecated": False},
    "/public_power": {
        "unix_seconds": [int(at(minutes=-15).timestamp()), *TS],
        "production_types": [
            {"name": "Solar", "data": [0.0, 0.0, 0.0]},
            {"name": "Renewable share of load", "data": [35.0, 99.0, 45.0]},
        ],
    },
    "/co2eq": {
        "unix_seconds": TS,
        "co2eq": [400.0, None],
        "co2eq_forecast": [None, 380.0],
        "deprecated": False,
    },
}


def client_with(handler) -> EnergyChartsClient:
    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return EnergyChartsClient(http=http, backoff=0)


async def test_fetch_parses_and_merges_co2_actual_and_forecast():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=RESPONSES[request.url.path])

    data = await client_with(handler).fetch(T0, at(1))
    assert data.price == {T0: 100.0, at(minutes=15): 80.5}
    assert data.ren_share[at(minutes=15)] == 45.0  # forecast gap filled by measured value
    assert data.co2 == {T0: 400.0, at(minutes=15): 380.0}
    price_url = next(u for u in seen if u.path == "/price")
    assert price_url.params["bzn"] == "DE-LU"  # the first prototype used the undocumented "county"
    assert next(u for u in seen if u.path == "/co2eq").params["country"] == "de"
    series = EnergySeries.from_points(data.price, data.ren_share, data.co2)
    assert series.ren_share[at(minutes=15)] == 45.0


async def test_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json=RESPONSES[request.url.path])

    client = client_with(handler)
    data = await client._get("/price", {})
    assert data["price"] == [100.0, 80.5]
    assert calls["n"] == 3


async def test_gives_up_after_retries():
    client = client_with(lambda r: httpx.Response(500))
    with pytest.raises(EnergyDataError):
        await client.fetch(T0, at(1))


async def test_malformed_payload_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(RESPONSES[request.url.path])
        if request.url.path == "/price":
            body["price"] = [1.0]  # length mismatch
        return httpx.Response(200, json=body)

    with pytest.raises(EnergyDataError):
        await client_with(handler).fetch(T0, at(1))


@pytest.mark.skipif(not os.environ.get("EFFICHARGE_LIVE_TESTS"), reason="network")
async def test_live_api():
    from datetime import UTC, datetime

    client = EnergyChartsClient()
    try:
        data = await client.fetch_window(datetime.now(UTC))
    finally:
        await client.aclose()
    assert len(data.price) >= 24
    assert len(data.ren_share) >= 24


async def test_measured_green_share_fills_the_past_forecast_wins_otherwise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=RESPONSES[request.url.path])

    data = await client_with(handler).fetch(at(minutes=-15), at(1))
    assert data.ren_share[at(minutes=-15)] == 35.0  # only measured: used
    assert data.ren_share[T0] == 40.0  # both: the forecast wins
    assert data.ren_share[at(minutes=15)] == 45.0  # forecast is None there: measured


async def test_missing_measured_share_does_not_break_the_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/public_power":
            return httpx.Response(500)
        return httpx.Response(200, json=RESPONSES[request.url.path])

    data = await client_with(handler).fetch(T0, at(1))
    assert data.ren_share == {T0: 40.0}
