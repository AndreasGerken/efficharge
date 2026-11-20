"""Runner + pure machine + real planner + simulated plug, on a manual clock."""

from __future__ import annotations

from datetime import datetime, timedelta

from efficharge.control.machine import (
    ChargeNow,
    EndReason,
    FinishCycle,
    Override,
    Phase,
    RecordOverride,
    Settings,
)
from efficharge.control.runner import DeviceRunner, InMemoryStore
from efficharge.devices.base import Capability
from efficharge.devices.simulated import ALL_CAPS, SimBattery, SimulatedAdapter
from efficharge.planner.window import Weights, best_window
from tests.helpers import T0, at, make_series

# Expensive now, cheap 15:00–19:00 UTC, expensive after.
SERIES = make_series([300.0] * 12 + [20.0] * 16 + [300.0] * 20)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def build(caps: Capability = ALL_CAPS):
    clock = Clock()
    adapter = SimulatedAdapter({"devices": [{"device_id": "d1", "name": "Bike"}]}, clock=clock)
    store = InMemoryStore()

    async def planner():
        def make_plan(plugged_at, override, now):
            deadline = override or plugged_at + timedelta(hours=12)
            return best_window(
                SERIES,
                now=now,
                deadline=deadline,
                duration=timedelta(hours=3),
                weights=Weights(1, 0),
            )

        return make_plan

    runner = DeviceRunner("d1", adapter, caps, store, Settings(), planner, clock)
    adapter.set_event_handler(runner.on_push)
    return clock, adapter, store, runner


async def advance(clock, runner, until: datetime, every=timedelta(minutes=1)):
    """Like the run loop: pushed events are handled immediately, then polling."""

    async def drain():
        while not runner.events.empty():
            await runner.handle(runner.events.get_nowait())

    await drain()
    while clock.now < until:
        clock.now = min(until, clock.now + every)
        for event in await runner.poll():
            await runner.handle(event)
        await drain()


async def test_full_cycle_with_simulated_plug():
    clock, adapter, store, runner = build()
    await runner.start()
    await adapter.plug_in("d1", SimBattery(soc=0.3, capacity_wh=400))
    await advance(clock, runner, at(0, 5))
    assert runner.state.phase is Phase.PLANNED
    assert runner.state.plan.start == at(3)  # cheap window
    assert adapter.devices["d1"].on is False
    assert adapter.devices["d1"].auto_on_at == at(3)  # dead-man's switch armed

    await advance(clock, runner, at(3, 5))
    assert runner.state.phase is Phase.CHARGING
    assert adapter.devices["d1"].on is True

    await advance(clock, runner, at(9))
    assert runner.state.phase in (Phase.DONE, Phase.IDLE)
    finished = [a for a in store.actions["d1"] if isinstance(a, FinishCycle)]
    assert finished and finished[0].reason is EndReason.FULL
    assert adapter.devices["d1"].battery.soc > 0.99


async def test_plug_button_starts_charging_and_is_recorded():
    clock, adapter, store, runner = build()
    await runner.start()
    await adapter.plug_in("d1")
    await advance(clock, runner, at(0, 5))
    assert runner.state.phase is Phase.PLANNED

    clock.now = at(0, 30)
    await adapter.press_button("d1")  # pushes SwitchChanged(on)
    await advance(clock, runner, at(0, 31))
    assert runner.state.phase is Phase.CHARGING
    assert RecordOverride(Override.BUTTON_ON, at(0, 30)) in store.actions["d1"]


async def test_button_detected_by_polling_without_push_support():
    caps = Capability.SWITCH | Capability.POWER_METER | Capability.SWITCH_STATE
    clock, adapter, store, runner = build(caps)
    adapter.set_event_handler(None)  # no pushes
    await runner.start()
    await adapter.plug_in("d1")
    await advance(clock, runner, at(0, 5))
    clock.now = at(0, 30)
    await adapter.press_button("d1")
    await advance(clock, runner, at(0, 32))
    assert runner.state.phase is Phase.CHARGING
    assert any(
        isinstance(a, RecordOverride) and a.kind is Override.BUTTON_ON for a in store.actions["d1"]
    )


async def test_device_timer_turns_plug_on_even_if_server_is_down():
    clock, adapter, _store, runner = build()
    await runner.start()
    await adapter.plug_in("d1")
    await advance(clock, runner, at(0, 5))
    # server "down" for hours: nobody polls
    clock.now = at(3, 10)
    await adapter.sync()
    assert adapter.devices["d1"].on is True
    assert adapter.devices["d1"].draw() > 0
    # after restart the runner sees the plug is on and continues as CHARGING
    runner.state = None
    runner.events = type(runner.events)()
    await runner.start()
    assert runner.state.phase is Phase.CHARGING


async def test_charge_now_command_from_ui():
    clock, adapter, _store, runner = build()
    await runner.start()
    await adapter.plug_in("d1")
    await advance(clock, runner, at(0, 5))
    runner.send(ChargeNow())
    await runner.handle(runner.events.get_nowait())
    assert runner.state.phase is Phase.CHARGING
    assert adapter.devices["d1"].on is True


async def test_plug_in_is_confirmed_quickly_after_a_first_reading():
    _clock, adapter, _store, runner = build()
    runner.poll_interval = timedelta(seconds=30)
    await runner.start()
    assert runner.next_poll_in() == timedelta(seconds=30)
    await adapter.plug_in("d1")
    for event in await runner.poll():
        await runner.handle(event)
    assert runner.state.above_count == 1
    assert runner.next_poll_in() == timedelta(seconds=5)  # second look soon


async def test_acknowledge_blinks_the_real_plug():
    clock, adapter, _store, runner = build()
    runner.ack_pause = timedelta(0)
    switched = []
    orig_on, orig_off = adapter.turn_on, adapter.turn_off

    async def on(device_id):
        switched.append("on")
        await orig_on(device_id)

    async def off(device_id, auto_on_at=None):
        switched.append("off")
        await orig_off(device_id, auto_on_at)

    adapter.turn_on, adapter.turn_off = on, off
    await runner.start()
    clock.now = at(3, 30)  # inside the cheap window: charging continues right away
    await adapter.plug_in("d1")
    await advance(clock, runner, at(3, 33))
    assert runner.state.phase is Phase.CHARGING
    assert switched == ["off", "on"]
    assert adapter.devices["d1"].on is True


async def test_sudden_drop_while_charging_is_checked_again_soon():
    clock, adapter, _store, runner = build()
    runner.poll_interval = timedelta(seconds=30)
    runner.ack_pause = timedelta(0)
    await runner.start()
    clock.now = at(3, 30)  # cheap window: charges right away
    await adapter.plug_in("d1")
    await advance(clock, runner, at(3, 36))  # detected at 3:32, then a few readings
    assert runner.state.phase is Phase.CHARGING
    assert runner.next_poll_in() == timedelta(seconds=30)  # normal pace while charging
    await adapter.unplug("d1")
    for event in await runner.poll():
        await runner.handle(event)
    assert runner.next_poll_in() == timedelta(seconds=5)  # sudden drop: confirm soon
