"""The demo's own clock and battery handling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from efficharge import demo_setup
from efficharge.control.supervisor import Supervisor
from efficharge.db.models import Device, DeviceAccount, DeviceStateRow
from efficharge.demo_setup import FAST, DemoClock, DemoFull, DemoManager
from efficharge.devices.simulated import ALL_CAPS, SimBattery

REAL = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


class FakeMonotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def mono(monkeypatch):
    fake = FakeMonotonic()
    monkeypatch.setattr(demo_setup.time, "monotonic", fake)
    return fake


def test_clock_runs_in_real_time_by_default(mono):
    clock = DemoClock(real=lambda: REAL)
    mono.t += 60
    assert clock() == REAL + timedelta(seconds=60)


def test_fast_forward_is_continuous(mono):
    clock = DemoClock(real=lambda: REAL)
    mono.t += 10
    before = clock()
    clock.set_speed(300)
    assert clock() == before  # no jump when switching
    mono.t += 2
    assert clock() == before + timedelta(minutes=10)
    clock.set_speed(1)
    mono.t += 1
    assert clock() == before + timedelta(minutes=10, seconds=1)


def test_fast_forward_stops_at_limit_then_runs_normally(mono):
    clock = DemoClock(real=lambda: REAL)
    clock.set_speed(300, limit=REAL + timedelta(hours=1))
    mono.t += 60  # would be +5 h
    assert clock() == REAL + timedelta(hours=1)
    assert clock.speed == 1
    mono.t += 5
    assert clock() == REAL + timedelta(hours=1, seconds=5)


def test_reset_goes_back_to_real_time(mono):
    clock = DemoClock(real=lambda: REAL)
    clock.set_speed(300)
    mono.t += 100
    clock.reset()
    assert clock() == REAL
    assert clock.speed == 1


async def test_full_battery_is_taken_off_and_idle_demos_deleted(db, vault):
    sup = Supervisor(
        db,
        vault,
        zone="DE-LU",
        clock=lambda: datetime.now(UTC),
        poll_interval=timedelta(milliseconds=20),
    )
    manager = DemoManager(
        db, vault, sup, zone="DE-LU", max_demos=2, take_off_after=timedelta(seconds=0)
    )
    try:
        demo = await manager.create()
        await manager.plug_in(demo)
        manager.sim_device(demo).battery = SimBattery(soc=1.0)
        await manager.step()  # notices "full"
        await manager.step()  # takes it off (take_off_after = 0)
        assert manager.sim_device(demo).battery is None

        await manager.create()
        with pytest.raises(DemoFull):
            await manager.create()

        demo.last_seen -= 3 * 3600  # idle for 3 h
        await manager.step()
        assert demo.device_id not in manager.demos
        assert demo.device_id not in sup.runners
        async with db() as s:  # really gone from the DB, children included
            assert await s.get(Device, demo.device_id) is None
            assert await s.get(DeviceAccount, demo.account_id) is None
            assert await s.get(DeviceStateRow, demo.device_id) is None
        await asyncio.sleep(0)
    finally:
        await manager.close()
        await sup.stop()


async def test_an_accounts_virtual_plug_starts_when_opened_and_starts_over_after_idle(db, vault):
    from sqlalchemy import select

    from efficharge.db.models import Cycle
    from efficharge.db.repo import add_account, add_device, create_user
    from efficharge.demo_setup import add_virtual_plug

    sup = Supervisor(
        db,
        vault,
        zone="DE-LU",
        clock=lambda: datetime.now(UTC),
        poll_interval=timedelta(milliseconds=20),
    )
    manager = DemoManager(db, vault, sup, zone="DE-LU", max_demos=1)
    async with db() as s, s.begin():
        user = await create_user(s, "rider@example.org", locale="de")
        plug = await add_virtual_plug(s, vault, user)
        real_account = await add_account(s, vault, user, "meross", {"token": "t"})
        real = await add_device(
            s, user, "Bike", account=real_account, vendor_device_id="m-1", capabilities=ALL_CAPS
        )
    try:
        assert plug.name == "Virtuelle Steckdose"
        await sup.start()
        assert plug.id not in sup.runners  # not started at boot
        assert await manager.open(real.id) is None  # a real plug has no simulation
        await manager.create()  # the one public demo allowed: doesn't block the account's

        demo = await manager.open(plug.id)
        assert demo is not None and not demo.temporary
        assert plug.id in sup.runners and sup.clock_for(plug.id) is demo.clock
        assert await manager.open(plug.id) is demo  # the running one
        await manager.plug_in(demo)
        async with db() as s, s.begin():
            s.add(Cycle(device_id=plug.id, plugged_at=demo.clock()))

        demo.last_seen -= 3 * 3600  # idle for 3 h: the simulation stops, the plug stays
        await manager.step()
        assert plug.id not in manager.demos and plug.id not in sup.runners
        async with db() as s:
            assert await s.get(Device, plug.id) is not None

        again = await manager.open(plug.id)  # opened again: it starts over
        assert again is not demo and manager.sim_device(again).battery is None
        async with db() as s:
            assert (await s.scalars(select(Cycle).where(Cycle.device_id == plug.id))).all() == []

        await manager.forget_account(plug.account_id)  # the user removed it
        assert plug.id not in manager.demos and plug.id not in sup.runners
    finally:
        await manager.close()
        await sup.stop()


async def test_plugging_in_fast_forwards_until_the_battery_comes_off(db, vault):
    sup = Supervisor(
        db,
        vault,
        zone="DE-LU",
        clock=lambda: datetime.now(UTC),
        poll_interval=timedelta(milliseconds=20),
    )
    manager = DemoManager(
        db, vault, sup, zone="DE-LU", max_demos=2, take_off_after=timedelta(seconds=0)
    )
    try:
        demo = await manager.create()
        await manager.plug_in(demo)  # a charge takes hours: the clock speeds up by itself
        assert demo.clock.speed == FAST and demo.auto_fast
        manager.sim_device(demo).battery = SimBattery(soc=1.0)
        await manager.step()  # notices "full"
        await manager.step()  # takes it off: the charge is over, normal speed again
        assert manager.sim_device(demo).battery is None
        assert demo.clock.speed == 1 and not demo.auto_fast

        await manager.plug_in(demo)
        await manager.set_speed(demo, 1.0)  # "Normal speed" by hand wins
        await manager.unplug(demo)
        assert demo.clock.speed == 1
        await manager.set_speed(demo, FAST)  # fast-forward by hand stays after a take-off
        await manager.plug_in(demo)
        await manager.unplug(demo)
        assert demo.clock.speed == FAST
    finally:
        await manager.close()
        await sup.stop()
