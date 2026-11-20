"""Terminal demo: the full loop (DB, real energy data, supervisor, runner, state
machine) with a simulated plug and battery on a sped-up clock.

    efficharge demo --speed 600      # 10 simulated minutes per real second
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from efficharge.control.supervisor import Supervisor
from efficharge.crypto import Vault, generate_key
from efficharge.db.migrate import upgrade
from efficharge.db.models import Cycle, DeviceStateRow
from efficharge.db.repo import add_account, add_device, create_user
from efficharge.db.session import make_engine, make_sessionmaker
from efficharge.devices.simulated import ALL_CAPS, SimBattery, SimulatedAdapter
from efficharge.energy.client import EnergyChartsClient
from efficharge.energy.fetcher import EnergyFetcher

BERLIN = ZoneInfo("Europe/Berlin")


class FastClock:
    """Simulated time that runs ``speed`` times faster than real time."""

    def __init__(self, speed: float, start: datetime | None = None) -> None:
        self.speed = speed
        self.t0 = start or datetime.now(UTC)
        self.m0 = time.monotonic()

    def __call__(self) -> datetime:
        return self.t0 + timedelta(seconds=(time.monotonic() - self.m0) * self.speed)


def _fmt(dt: datetime | None) -> str:
    return dt.astimezone(BERLIN).strftime("%a %H:%M") if dt else "-"


async def run_demo(db: Path, speed: float, sim_hours: float, zone: str = "DE-LU") -> None:
    db.unlink(missing_ok=True)
    url = f"sqlite+aiosqlite:///{db}"
    await asyncio.to_thread(upgrade, url)
    engine = make_engine(url)
    sessions = make_sessionmaker(engine)
    vault = Vault.from_config(f"demo:{generate_key()}")
    clock = FastClock(speed)

    async with sessions() as s, s.begin():
        user = await create_user(s, "demo@efficharge.local", is_demo=True)
        auth = await SimulatedAdapter.login({})
        account = await add_account(s, vault, user, "simulated", auth)
        device = await add_device(
            s,
            user,
            "Demo e-bike charger",
            account=account,
            vendor_device_id="sim-1",
            capabilities=ALL_CAPS,
        )
        device_id = device.id

    print("Fetching real prices and grid data from energy-charts.info …")
    client = EnergyChartsClient(bidding_zone=zone)
    fetcher = EnergyFetcher(client, sessions, zone, lambda: datetime.now(UTC))
    await fetcher.fetch_once()

    adapters: list[SimulatedAdapter] = []

    def factory(kind, auth):
        adapter = SimulatedAdapter(auth, clock=clock)
        adapters.append(adapter)
        return adapter

    # The runner polls every 30 *simulated* seconds; never faster than 5x per real second.
    poll = timedelta(seconds=max(0.2, 30 / speed))
    sup = Supervisor(
        sessions, vault, zone=zone, clock=clock, adapter_factory=factory, poll_interval=poll
    )
    await sup.start()
    sim = adapters[0]
    rng = random.Random(1)
    next_plug = clock() + timedelta(minutes=5)
    unplug_at: datetime | None = None
    end = clock() + timedelta(hours=sim_hours)
    print(
        f"Simulating {sim_hours:.0f} h at {speed:.0f}x. Plug and battery are fake, prices real.\n"
    )
    try:
        while clock() < end:
            now = clock()
            dev = sim.devices["sim-1"]
            if dev.battery is None and next_plug and now >= next_plug:
                soc = rng.uniform(0.15, 0.6)
                await sim.plug_in("sim-1", SimBattery(soc=soc))
                print(f"{_fmt(now)}  🔌 battery plugged in ({soc:.0%})")
                next_plug = None
            if dev.battery is not None and dev.battery.soc >= 0.995 and unplug_at is None:
                unplug_at = now + timedelta(hours=1)
            if unplug_at and now >= unplug_at:
                await sim.unplug("sim-1")
                print(f"{_fmt(now)}  ↩ battery taken off the charger")
                unplug_at = None
                next_plug = now + timedelta(hours=rng.uniform(4, 14))
            async with sessions() as s:
                row = await s.get(DeviceStateRow, device_id)
            plan = (row.data or {}).get("plan") if row else None
            window = (
                f"window {_fmt(datetime.fromisoformat(plan['start']))}–"
                f"{_fmt(datetime.fromisoformat(plan['end']))} ({plan['reason']})"
                if plan
                else ""
            )
            soc = f"{dev.battery.soc:4.0%}" if dev.battery else "  - "
            print(
                f"\r{_fmt(now)}  {row.phase if row else '?':15} plug {'ON ' if dev.on else 'OFF'}"
                f" {dev.draw():6.1f} W  battery {soc}  {window}   ",
                end="",
                flush=True,
            )
            await asyncio.sleep(0.5)
    finally:
        print()
        await sup.stop()
        await client.aclose()

    async with sessions() as s:
        cycles = (await s.scalars(select(Cycle).order_by(Cycle.id))).all()
    print(f"\n{len(cycles)} cycle(s):")
    for c in cycles:
        saved = (
            f"{(c.cost_baseline_eur - c.cost_actual_eur) * 100:+.1f} ct saved"
            if c.cost_actual_eur is not None and c.cost_baseline_eur is not None
            else "€ n/a (outside known prices)"
        )
        co2 = (
            f", {c.co2_baseline_g - c.co2_actual_g:+.0f} g CO₂ avoided"
            if c.co2_actual_g is not None and c.co2_baseline_g is not None
            else ""
        )
        print(
            f"  plugged {_fmt(c.plugged_at)}, charged {_fmt(c.actual_start)}–{_fmt(c.end_at)}"
            f" ({c.end_reason or 'running'}), {c.kwh or 0:.2f} kWh, {saved}{co2}"
        )
    await engine.dispose()


def main(db: Path, speed: float, hours: float) -> None:
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_demo(db, speed, hours))
