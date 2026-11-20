"""DB layer: migrations, UTC handling, energy store, serialisation, planner inputs."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import select

from efficharge.control.machine import DeviceState, Phase
from efficharge.control.planning import build_planner, past_durations
from efficharge.control.serialize import (
    plan_from_dict,
    plan_to_dict,
    state_from_dict,
    state_to_dict,
)
from efficharge.db.migrate import alembic_config
from efficharge.db.models import Cycle, DeviceAccount, EnergyDataRow
from efficharge.db.repo import account_auth, add_account, add_device, create_user
from efficharge.devices.simulated import ALL_CAPS
from efficharge.energy import store
from efficharge.energy.client import RawEnergyData
from efficharge.planner.window import Weights, best_window
from tests.helpers import T0, at, make_series

ZONE = "DE-LU"


async def test_models_match_migrations(db, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    # raises if the models changed without a new migration
    await asyncio.to_thread(command.check, alembic_config(url))


async def test_datetimes_come_back_aware_utc(db):
    async with db() as s, s.begin():
        s.add(EnergyDataRow(zone=ZONE, ts=at(1).astimezone(), price_eur_mwh=1.0))
    async with db() as s:
        row = (await s.scalars(select(EnergyDataRow))).one()
    assert row.ts == at(1)
    assert row.ts.tzinfo is UTC


async def test_naive_datetimes_are_refused(db):
    async with db() as s:
        s.add(EnergyDataRow(zone=ZONE, ts=datetime(2026, 1, 1), price_eur_mwh=1.0))  # noqa: DTZ001
        with pytest.raises(Exception, match="naive"):
            await s.flush()


async def test_account_token_encrypted_at_rest(db, vault):
    async with db() as s, s.begin():
        user = await create_user(s, " Erika@Example.org ")
        acc = await add_account(s, vault, user, "meross", {"token": "tok-123"})
    async with db() as s:
        stored = await s.get(DeviceAccount, acc.id)
        assert "tok-123" not in stored.auth_encrypted
        assert account_auth(vault, stored) == {"token": "tok-123"}
        assert user.email == "erika@example.org"


async def test_energy_upsert_keeps_values_and_reports_new_prices(db):
    first = RawEnergyData(price={T0: 100.0, at(1): 200.0}, ren_share={T0: 40.0}, co2={})
    async with db() as s, s.begin():
        assert await store.upsert(s, ZONE, first) is True
    # price horizon unchanged, but the green share now reaches 13:00 → replan too
    second = RawEnergyData(price={T0: 100.0}, ren_share={at(1): 60.0}, co2={T0: 400.0})
    async with db() as s, s.begin():
        assert await store.upsert(s, ZONE, second) is True
    later = RawEnergyData(price={at(2): 50.0}, ren_share={}, co2={})
    async with db() as s, s.begin():
        assert await store.upsert(s, ZONE, later) is True
    # only the green-share forecast grows: also a reason to replan
    green = RawEnergyData(price={}, ren_share={at(3): 70.0}, co2={})
    async with db() as s, s.begin():
        assert await store.upsert(s, ZONE, green) is True
    async with db() as s:
        series = await store.load_series(s, ZONE, T0, at(3))
    # hourly input was stored expanded to the 15-min grid
    assert series.price[at(minutes=45)] == 100.0
    assert series.price[at(1, 15)] == 200.0
    assert series.ren_share[at(1)] == 60.0
    assert series.co2[T0] == 400.0
    assert series.price[at(2)] == 50.0


def test_state_and_plan_roundtrip():
    plan = best_window(
        make_series([50] * 4 + [10] * 8),
        now=T0,
        deadline=at(3),
        duration=timedelta(hours=1),
        weights=Weights(1, 0),
    )
    state = DeviceState(
        phase=Phase.PLANNED,
        since=T0,
        switch_on=False,
        plugged_at=T0,
        plan=plan,
        expected_switch=False,
        expected_until=at(0, 1),
    )
    assert plan_from_dict(plan_to_dict(plan)) == plan
    assert state_from_dict(state_to_dict(state)) == state
    assert state_from_dict(None) == DeviceState()


async def _device(db, vault, price_weight=0.5, **settings):
    async with db() as s, s.begin():
        user = await create_user(s, "u@example.org", price_weight=price_weight)
        acc = await add_account(s, vault, user, "simulated", {"devices": []})
        dev = await add_device(
            s, user, "Bike", account=acc, vendor_device_id="d1", capabilities=ALL_CAPS, **settings
        )
    return dev.id


async def test_duration_history_skips_paused_and_unplugged(db, vault):
    dev = await _device(db, vault)
    async with db() as s, s.begin():
        for i, (hours, reason, override) in enumerate(
            [
                (2, "full", None),
                (9, "full", "button_off"),
                (3, "unplugged", None),
                (4, "full", "button_on"),
            ]
        ):
            start = at(24 * i)
            s.add(
                Cycle(
                    device_id=dev,
                    plugged_at=start,
                    actual_start=start,
                    end_at=start + timedelta(hours=hours),
                    end_reason=reason,
                    override=override,
                    kwh=0.3,
                )
            )
    async with db() as s:
        assert await past_durations(s, dev) == [timedelta(hours=2), timedelta(hours=4)]


async def test_build_planner_uses_rules_timezone_and_weights(db, vault):
    # user estimate 40 min → planned 40 min (no history yet)
    dev = await _device(db, vault, price_weight=1.0, default_duration_min=40)
    prices = {at(minutes=15 * i): (10.0 if 8 <= i < 12 else 100.0) for i in range(40)}
    async with db() as s, s.begin():
        await store.upsert(s, ZONE, RawEnergyData(price=prices, ren_share={}, co2={}))
    async with db() as s:
        make_plan = await build_planner(s, dev, ZONE, now=T0)
    # Thu 5 Nov 2026 12:00 UTC = 13:00 Berlin → default rule "06:00–15:00 → 17:00"
    plan = make_plan(T0, None, T0)
    assert plan.deadline == datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
    assert plan.start == at(2)
    assert plan.duration == timedelta(minutes=40)  # the user's estimate, as is
    # one-off override
    assert make_plan(T0, at(1, 30), T0).deadline == at(1, 30)


async def test_remaining_time_learns_from_past_charges(db, vault):
    from efficharge.control.machine import DeviceState, Phase
    from efficharge.db.models import PowerSampleRow
    from efficharge.web.data import remaining_time

    dev = await _device(db, vault)

    def tail_charge(start, cc_min, tail_min=90):
        """Plateau at 180 W, exponential decline (40 min), switch-off at ~20 W."""
        pts = [(start + timedelta(minutes=m), 180.0) for m in range(cc_min)]
        for m in range(tail_min):
            w = 171 * math.exp(-m / 40)
            if w < 20:
                break
            pts.append((start + timedelta(minutes=cc_min + m), w))
        return pts

    async with db() as s, s.begin():
        past = tail_charge(at(-24), 90)
        s.add(
            Cycle(
                device_id=dev,
                plugged_at=at(-24),
                actual_start=at(-24),
                end_at=at(-24) + timedelta(minutes=176),  # switch-off after ~86 min decline
                end_reason="full",
                kwh=0.33,
            )
        )
        running = tail_charge(T0, 60)[: 60 + 10]  # 10 min into the tail
        for ts, w in past + running:
            s.add(PowerSampleRow(device_id=dev, ts=ts, watts=w))
        current = Cycle(device_id=dev, plugged_at=T0, actual_start=T0)
        s.add(current)
    now = running[-1][0]
    async with db() as s:
        result = await remaining_time(s, dev, current, DeviceState(phase=Phase.CHARGING), now)
    # 10 min into the decline (133 W): extrapolated down to the switch-off power learned
    # from the past charge (~20 W): 40 · ln(133/20) ≈ 76 min
    assert result.kind == "decline"
    assert result.left.total_seconds() == pytest.approx(76 * 60, abs=3 * 60)


async def test_a_longer_green_forecast_triggers_a_replan(db):
    """Eco first plans only where the green forecast is known: when it grows, replan."""
    prices = {at(minutes=15 * i): 100.0 for i in range(48)}

    def raw(green_hours: int) -> RawEnergyData:
        green = {at(minutes=15 * i): 50.0 for i in range(green_hours * 4)}
        return RawEnergyData(price=prices, ren_share=green, co2={})

    async with db() as s, s.begin():
        assert await store.upsert(s, "DE-LU", raw(4))  # first data
    async with db() as s, s.begin():
        assert not await store.upsert(s, "DE-LU", raw(4))  # nothing new
    async with db() as s, s.begin():
        assert await store.upsert(s, "DE-LU", raw(8))  # forecast grew, prices didn't
