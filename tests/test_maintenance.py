"""Nightly maintenance: settle finished charges, delete old idle readings."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from efficharge.control.maintenance import KEEP_IDLE_SAMPLES, SETTLED_AFTER, maintain
from efficharge.control.store import DbStateStore
from efficharge.db.models import Cycle, PowerSampleRow
from efficharge.db.repo import add_account, add_device, create_user
from efficharge.devices.simulated import ALL_CAPS
from efficharge.energy import store as energy_store
from efficharge.energy.client import RawEnergyData
from tests.helpers import at

ZONE = "DE-LU"


async def setup(db, vault):
    async with db() as s, s.begin():
        user = await create_user(s, "m@example.org")
        acc = await add_account(s, vault, user, "simulated", {"devices": []})
        dev = await add_device(
            s, user, "Bike", account=acc, vendor_device_id="sim-1", capabilities=ALL_CAPS
        )
        cycle = Cycle(
            device_id=dev.id,
            plugged_at=at(0),
            actual_start=at(1),
            end_at=at(2),
            end_reason="full",
        )
        s.add(cycle)
        # idle before, charging during, idle after
        for minutes in range(-60, 180, 5):
            s.add(PowerSampleRow(device_id=dev.id, ts=at(minutes=minutes), watts=100.0))
    return cycle.id


async def put_energy(db, co2: float):
    slots = {at(minutes=15 * i): 0.0 for i in range(-8, 16)}
    async with db() as s, s.begin():
        await energy_store.upsert(
            s,
            ZONE,
            RawEnergyData(
                price={t: 100.0 for t in slots},
                ren_share={},
                co2={t: co2 for t in slots},
            ),
        )


async def test_charge_is_settled_with_the_final_energy_data(db, vault):
    cycle_id = await setup(db, vault)
    await put_energy(db, co2=400.0)
    store = DbStateStore(db, ZONE)

    await maintain(store, at(2) + SETTLED_AFTER / 2)  # too early: data may still change
    async with db() as s:
        assert (await s.get(Cycle, cycle_id)).finalised is False

    await put_energy(db, co2=200.0)  # actuals replaced the forecast
    await maintain(store, at(2) + SETTLED_AFTER + timedelta(minutes=1))
    async with db() as s:
        cycle = await s.get(Cycle, cycle_id)
    assert cycle.finalised is True
    assert cycle.kwh is not None and cycle.kwh > 0
    assert cycle.co2_actual_g is not None
    assert abs(cycle.co2_actual_g - 200.0 * cycle.kwh) < 1e-6


async def test_old_idle_readings_go_charge_readings_stay(db, vault):
    await setup(db, vault)
    store = DbStateStore(db, ZONE)

    await maintain(store, at(-1) + KEEP_IDLE_SAMPLES - timedelta(minutes=1))
    async with db() as s:
        assert len((await s.scalars(select(PowerSampleRow))).all()) == 48  # nothing old enough

    await maintain(store, at(3) + KEEP_IDLE_SAMPLES)
    async with db() as s:
        left = (await s.scalars(select(PowerSampleRow.ts).order_by(PowerSampleRow.ts))).all()
    assert left[0] == at(0) and left[-1] == at(2)  # from plug-in to the end of the charge
    assert len(left) == 25
