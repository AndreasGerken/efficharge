"""Async client for the energy-charts.info API (Fraunhofer ISE).

Data licence: CC BY 4.0. Prices come from Bundesnetzagentur | SMARD.de.
Attribution is required wherever the data is shown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from efficharge.energy.series import from_unix

logger = logging.getLogger(__name__)

BASE_URL = "https://api.energy-charts.info"
ATTRIBUTION = "Energy-Charts (Fraunhofer ISE), data: Bundesnetzagentur | SMARD.de, CC BY 4.0"


class EnergyDataError(Exception):
    """The API answered, but with something we can't use."""


@dataclass(frozen=True)
class RawEnergyData:
    price: dict[datetime, float | None]
    ren_share: dict[datetime, float | None]
    co2: dict[datetime, float | None]


class EnergyChartsClient:
    def __init__(
        self,
        *,
        bidding_zone: str = "DE-LU",
        country: str = "de",
        http: httpx.AsyncClient | None = None,
        retries: int = 3,
        backoff: float = 2.0,
    ) -> None:
        self.bidding_zone = bidding_zone
        self.country = country
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=20.0)
        self.retries = retries
        self.backoff = backoff

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str | int]) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = await self._http.get(path, params=params)
                response.raise_for_status()
                data = response.json()
                if data.get("deprecated"):
                    logger.warning("energy-charts endpoint %s is marked deprecated", path)
                return data
            except (httpx.HTTPError, ValueError) as e:
                last_error = e
                logger.warning("energy-charts %s failed (attempt %d): %s", path, attempt + 1, e)
                if attempt + 1 < self.retries:
                    await asyncio.sleep(self.backoff * 2**attempt)
        raise EnergyDataError(f"{path} failed after {self.retries} attempts") from last_error

    @staticmethod
    def _series(data: dict, key: str) -> dict[datetime, float | None]:
        seconds, values = data.get("unix_seconds"), data.get(key)
        if not isinstance(seconds, list) or not isinstance(values, list):
            raise EnergyDataError(f"missing unix_seconds/{key}")
        if len(seconds) != len(values):
            raise EnergyDataError(f"length mismatch for {key}")
        return from_unix(seconds, values)

    async def fetch(self, start: datetime, end: datetime) -> RawEnergyData:
        span = {"start": int(start.timestamp()), "end": int(end.timestamp())}
        price, ren, co2, actual = await asyncio.gather(
            self._get("/price", {"bzn": self.bidding_zone, **span}),
            self._get("/ren_share_forecast", {"country": self.country, **span}),
            self._get("/co2eq", {"country": self.country, **span}),
            self._actual_ren_share(span),
        )
        co2_actual = self._series(co2, "co2eq")
        # Use the actual value where it exists, the forecast otherwise.
        co2_forecast = self._series(co2, "co2eq_forecast") if "co2eq_forecast" in co2 else {}
        merged_co2 = {
            t: v if v is not None else co2_forecast.get(t)
            for t, v in (co2_forecast | co2_actual).items()
        }
        # The forecast only starts at today's midnight. For earlier slots use the measured
        # share (same definition: renewable share of load; within ~2 points of the forecast).
        forecast = self._series(ren, "ren_share")
        ren_share = {t: v for t, v in actual.items() if v is not None} | {
            t: v for t, v in forecast.items() if v is not None
        }
        return RawEnergyData(
            price=self._series(price, "price"),
            ren_share=ren_share,
            co2=merged_co2,
        )

    async def fetch_past(self, start: datetime, end: datetime) -> RawEnergyData:
        """Past days only: measured green share and CO₂ (there is no forecast for them)."""
        span = {"start": int(start.timestamp()), "end": int(end.timestamp())}
        price = await self._get("/price", {"bzn": self.bidding_zone, **span})
        co2 = await self._get("/co2eq", {"country": self.country, **span})
        ren_share = await self._actual_ren_share(span)
        if not ren_share:
            raise EnergyDataError("no measured green-power share for this span")
        return RawEnergyData(
            price=self._series(price, "price"),
            ren_share={t: v for t, v in ren_share.items() if v is not None},
            co2=self._series(co2, "co2eq"),
        )

    async def _actual_ren_share(self, span: dict[str, int]) -> dict[datetime, float | None]:
        """Measured renewable share of load (``public_power``). Best effort: the forecast
        alone is enough for planning, so a failure here must not fail the whole fetch."""
        try:
            data = await self._get("/public_power", {"country": self.country, **span})
            for production in data.get("production_types", []):
                if production.get("name") == "Renewable share of load":
                    return from_unix(data["unix_seconds"], production["data"])
        except (EnergyDataError, KeyError, TypeError, ValueError) as e:
            logger.info("no measured renewable share: %s", e)
        return {}

    async def fetch_window(
        self,
        now: datetime,
        past: timedelta = timedelta(hours=6),
        future: timedelta = timedelta(days=2),
    ) -> RawEnergyData:
        return await self.fetch(now - past, now + future)
