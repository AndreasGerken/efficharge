"""Supervisor + DB store + simulated plug: the slice-3 loop without the web app."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from efficharge.control.machine import ChargeNow
from efficharge.control.supervisor import Supervisor
from efficharge.db.models import Cycle, DeviceAccount, DeviceStateRow, PowerSampleRow
from efficharge.db.repo import add_account, add_device, create_user
from efficharge.devices.base import AuthenticationFailed
from efficharge.devices.simulated import ALL_CAPS, SimulatedAdapter
from efficharge.energy import store
from efficharge.energy.client import RawEnergyData
from tests.helpers import T0, at

ZONE = "DE-LU"
FAST = timedelta(milliseconds=5)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


async def setup(db, vault, adapter_cls=SimulatedAdapter):
    clock = Clock()
    # expensive now, cheap 13:00–16:00 UTC (= before the default 17:00 Berlin deadline)
    prices = {at(minutes=15 * i): (20.0 if 4 <= i < 16 else 300.0) for i in range(48)}
    co2 = {t: (150.0 if p < 100 else 450.0) for t, p in prices.items()}
    async with db() as s, s.begin():
        await store.upsert(s, ZONE, RawEnergyData(price=prices, ren_share={}, co2=co2))
        user = await create_user(s, "demo@example.org", price_weight=1.0)  # cost first
        # stands in for a real plug account (a "simulated" one is a virtual plug, which
        # starts only when opened); the factory below gives it the simulated adapter
        acc = await add_account(
            s, vault, user, "test-plug", {"devices": [{"device_id": "sim-1", "name": "Bike"}]}
        )
        dev = await add_device(
            s,
            user,
            "Bike",
            account=acc,
            vendor_device_id="sim-1",
            capabilities=ALL_CAPS,
            default_duration_min=180,  # 3 h planned
        )
    adapters = []

    def factory(kind, auth):
        adapters.append(adapter_cls(auth, clock=clock))
        return adapters[-1]

    sup = Supervisor(
        db,
        vault,
        zone=ZONE,
        clock=clock,
        adapter_factory=factory,
        poll_interval=FAST,
        max_backoff=0.01,
    )
    return sup, clock, adapters, dev.id, acc.id


async def run_until(clock, until: datetime, step=timedelta(minutes=1)):
    """Advance simulated time; give the runner a few loop turns per step."""
    while clock.now < until:
        clock.now += step
        await asyncio.sleep(0.02)


async def phase(db, dev_id) -> str:
    async with db() as s:
        return (await s.get(DeviceStateRow, dev_id)).phase


async def wait_phase(db, dev_id, expected: str, timeout: float = 5.0) -> None:
    """The runner works in real time next to the simulated clock: on a busy machine it
    may need a moment longer to catch up. Wait for it (no fixed sleeps)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while (current := await phase(db, dev_id)) != expected:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"phase {current!r}, expected {expected!r}")
        await asyncio.sleep(0.02)


async def test_cycle_is_planned_charged_and_accounted(db, vault):
    sup, clock, adapters, dev_id, _ = await setup(db, vault)
    await sup.start()
    try:
        sim = adapters[0]
        await sim.plug_in("sim-1")
        await run_until(clock, at(0, 10))
        await wait_phase(db, dev_id, "planned")
        assert sim.devices["sim-1"].auto_on_at == at(1)
        await run_until(clock, at(10), step=timedelta(minutes=2))
        for _ in range(250):  # let the runner catch up with the clock (busy machine)
            if await phase(db, dev_id) in ("done", "idle"):
                break
            await asyncio.sleep(0.02)
        assert await phase(db, dev_id) in ("done", "idle")
    finally:
        await sup.stop()

    async with db() as s:
        cycle = (await s.scalars(select(Cycle))).one()
        samples = (await s.scalars(select(PowerSampleRow))).all()
    assert cycle.planned_start == at(1)
    assert cycle.actual_start is not None and cycle.actual_start >= at(1)
    assert cycle.end_reason == "full"
    assert cycle.kwh > 0.3
    assert cycle.cost_baseline_eur > cycle.cost_actual_eur  # charged in the cheap hours
    assert cycle.co2_baseline_g > cycle.co2_actual_g
    assert len(samples) > 10


async def test_state_survives_supervisor_restart(db, vault):
    sup, clock, adapters, dev_id, _ = await setup(db, vault)
    await sup.start()
    await adapters[0].plug_in("sim-1")
    await run_until(clock, at(0, 10))
    await wait_phase(db, dev_id, "planned")  # before stopping: let the runner catch up
    sim_device = adapters[0].devices["sim-1"]
    await sup.stop()

    # "server restart": new supervisor, new adapter instance with the same plug state
    sup2, _, adapters2, _, _ = sup, clock, [], dev_id, None
    sup2 = Supervisor(
        db,
        vault,
        zone=ZONE,
        clock=clock,
        poll_interval=FAST,
        adapter_factory=lambda k, a: (
            adapters2.append(SimulatedAdapter(a, clock=clock)) or adapters2[-1]
        ),
    )
    await sup2.start()
    try:
        adapters2[0].devices["sim-1"] = sim_device
        await run_until(clock, at(0, 20))
        await wait_phase(db, dev_id, "planned")  # not switched on by the restart
        assert sim_device.on is False
        sup2.send(dev_id, ChargeNow())
        await run_until(clock, at(0, 22))
        await wait_phase(db, dev_id, "charging")
        assert sim_device.on is True
    finally:
        await sup2.stop()


class FlakyAdapter(SimulatedAdapter):
    calls = 0

    async def get_power(self, device_id):
        FlakyAdapter.calls += 1
        if FlakyAdapter.calls == 2:
            raise RuntimeError("boom")
        return await super().get_power(device_id)


async def test_crashed_runner_is_restarted(db, vault):
    FlakyAdapter.calls = 0
    sup, clock, adapters, dev_id, _ = await setup(db, vault, FlakyAdapter)
    await sup.start()
    try:
        await adapters[0].plug_in("sim-1")
        # generous: the restart backoff plus a few polls must fit in, even on a busy machine
        await run_until(clock, at(0, 30))
        assert FlakyAdapter.calls > 3
        await wait_phase(db, dev_id, "planned")
    finally:
        await sup.stop()


class ExpiredTokenAdapter(SimulatedAdapter):
    async def get_state(self, device_id):
        raise AuthenticationFailed("token expired")


async def test_auth_failure_marks_account_and_stops(db, vault):
    sup, clock, _, dev_id, acc_id = await setup(db, vault, ExpiredTokenAdapter)
    await sup.start()
    try:
        await run_until(clock, at(0, 2))
        assert sup.tasks[dev_id].done()
    finally:
        await sup.stop()
    async with db() as s:
        assert (await s.get(DeviceAccount, acc_id)).status == "needs_reauth"
