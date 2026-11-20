"""DeviceRunner: the I/O shell around the pure state machine.

One runner per device. It turns the outside world into events (polls, pushes,
UI commands), calls ``machine.step`` and executes the resulting actions:
switch commands go to the adapter, everything else goes to the ``StateStore``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import NoReturn, Protocol

from efficharge.control.machine import (
    Acknowledge,
    Action,
    Context,
    DeviceState,
    Event,
    Phase,
    Planner,
    PowerSample,
    Settings,
    Startup,
    SwitchState,
    Tick,
    TurnOff,
    TurnOn,
    needs_planner,
    step,
)
from efficharge.devices.base import (
    Capability,
    DeviceAdapter,
    DeviceUnavailable,
    PowerReading,
    SwitchChanged,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _no_planner(*_args: object) -> NoReturn:
    raise RuntimeError("planner used although needs_planner() said no")


#: second look after a possible plug-in (see ``DeviceRunner.next_poll_in``)
CONFIRM_AFTER = timedelta(seconds=5)


class StopRunner:
    """Leave the loop after the current step: a cancel could cut a database write in half
    and leave its transaction holding the lock (see ``Supervisor.stop_device``)."""


class PollNow:
    """UI "refresh": read the plug right away (handled by the runner, not the machine)."""


class StateStore(Protocol):
    """Persistence for one device. Implemented by the DB layer (and in memory for tests)."""

    async def load(self, device_id: str) -> DeviceState: ...

    async def save(self, device_id: str, state: DeviceState, actions: list[Action]) -> None:
        """Persist the new state together with the bookkeeping actions
        (StartCycle, SavePlan, RecordOverride, MarkCharging, FinishCycle) atomically."""
        ...

    async def record_power(self, device_id: str, reading: PowerReading) -> None: ...


class InMemoryStore:
    def __init__(self) -> None:
        self.states: dict[str, DeviceState] = {}
        self.actions: dict[str, list[Action]] = {}
        self.power: dict[str, list[PowerReading]] = {}

    async def load(self, device_id: str) -> DeviceState:
        return self.states.get(device_id, DeviceState())

    async def save(self, device_id: str, state: DeviceState, actions: list[Action]) -> None:
        self.states[device_id] = state
        self.actions.setdefault(device_id, []).extend(actions)

    async def record_power(self, device_id: str, reading: PowerReading) -> None:
        self.power.setdefault(device_id, []).append(reading)


class DeviceRunner:
    def __init__(
        self,
        device_id: str,
        adapter: DeviceAdapter,
        capabilities: Capability,
        store: StateStore,
        settings: Settings,
        planner: Callable[[], Awaitable[Planner]],
        clock: Clock,
        *,
        poll_interval: timedelta = timedelta(seconds=30),
        switch_retries: int = 3,
        retry_delay: float = 2.0,
        vendor_id: str | None = None,
        ack_pause: timedelta = timedelta(seconds=2),
    ) -> None:
        #: our id (DB / store); ``vendor_id`` is the id the adapter knows the plug by
        self.device_id = device_id
        self.vendor_id = vendor_id or device_id
        self.ack_pause = ack_pause
        self.adapter = adapter
        self.caps = capabilities
        self.store = store
        self.settings = settings
        #: Builds a fresh planner (with current energy data, rules, history) per step.
        self.planner = planner
        self.clock = clock
        self.poll_interval = poll_interval
        self.switch_retries = switch_retries
        self.retry_delay = retry_delay
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.state: DeviceState | None = None

    # ── inputs ──

    def send(self, event: Event) -> None:
        """Queue a UI command or pushed event. Call from within the event loop."""
        self.events.put_nowait(event)

    async def on_push(self, device_id: str, event: SwitchChanged | PowerReading) -> None:
        """Adapter push callback (register via ``adapter.set_event_handler``)."""
        if device_id != self.vendor_id:
            return
        if isinstance(event, SwitchChanged):
            self.send(SwitchState(event.on))
        else:
            await self.store.record_power(self.device_id, event)
            self.send(PowerSample(event.watts))

    async def poll(self) -> list[Event]:
        events: list[Event] = []
        if self.caps & Capability.SWITCH_STATE:
            events.append(SwitchState(await self.adapter.get_state(self.vendor_id)))
        if self.caps & Capability.POWER_METER:
            reading = await self.adapter.get_power(self.vendor_id)
            await self.store.record_power(self.device_id, reading)
            events.append(PowerSample(reading.watts))
        events.append(Tick())
        return events

    # ── core ──

    async def handle(self, event: Event) -> list[Action]:
        if self.state is None:
            self.state = await self.store.load(self.device_id)
        make_plan = (
            await self.planner() if needs_planner(self.state, event, self.settings) else _no_planner
        )
        ctx = Context(now=self.clock(), settings=self._settings_now(), make_plan=make_plan)
        new_state, actions = step(self.state, event, ctx)
        for action in actions:
            await self._execute(action)
        bookkeeping = [a for a in actions if not isinstance(a, TurnOn | TurnOff | Acknowledge)]
        await self.store.save(self.device_id, new_state, bookkeeping)
        self.state = new_state
        if actions:
            logger.info(
                "%s: %s -> %s %s",
                self.device_id,
                type(event).__name__,
                new_state.phase,
                [type(a).__name__ for a in actions],
            )
        return actions

    async def _execute(self, action: Action) -> None:
        if isinstance(action, TurnOn):
            await self._retry(lambda: self.adapter.turn_on(self.vendor_id))
        elif isinstance(action, TurnOff):
            auto_on = action.auto_on_at if self.caps & Capability.DEVICE_TIMER else None
            await self._retry(lambda: self.adapter.turn_off(self.vendor_id, auto_on))
        elif isinstance(action, Acknowledge):
            # off – pause – on: visible/audible "battery noticed". Must end ON: if switching
            # off fails we skip the rest (it's still on); "on" is retried like any TurnOn.
            try:
                await self._retry(lambda: self.adapter.turn_off(self.vendor_id, None))
            except DeviceUnavailable:
                return
            await asyncio.sleep(self.ack_pause.total_seconds())
            await self._retry(lambda: self.adapter.turn_on(self.vendor_id))

    async def _retry(self, command: Callable[[], Awaitable[None]]) -> None:
        """Retry switch commands. If they keep failing, the exception propagates; the
        supervisor restarts the runner and ``Startup`` reconciles (when in doubt: ON)."""
        for attempt in range(self.switch_retries):
            try:
                await command()
                return
            except DeviceUnavailable:
                if attempt + 1 == self.switch_retries:
                    raise
                await asyncio.sleep(self.retry_delay * 2**attempt)

    async def start(self) -> None:
        observed: bool | None = None
        if self.caps & Capability.SWITCH_STATE:
            try:
                observed = await self.adapter.get_state(self.vendor_id)
            except DeviceUnavailable:
                observed = None
        await self.handle(Startup(switch_on=observed))

    def next_poll_in(self) -> timedelta:
        """Normally ``poll_interval``. After a first sign of a plug-in or of the battery being
        taken off, look again soon: confirmed in seconds instead of a whole interval."""
        st = self.state
        if st is not None and (
            (st.phase is Phase.IDLE and st.above_count > 0)
            or (st.phase is Phase.CHARGING and st.drop_since is not None)
        ):
            return min(self.poll_interval, CONFIRM_AFTER)
        return self.poll_interval

    def _settings_now(self):
        """The demo runs its clock faster (``speed``). Times in the settings are meant as
        real seconds where they wait for the plug (the echo of our own switch command):
        at 300× a 15 s echo window would be 0.05 real seconds, and our own switching
        would look like a button press. So that one scales with the speed."""
        speed = getattr(self.clock, "speed", 1.0)
        if speed <= 1:
            return self.settings
        return replace(self.settings, echo_window=self.settings.echo_window * speed)

    async def run(self) -> None:
        """Main loop: pushed/UI events immediately, polling every ``poll_interval``."""
        await self.start()
        while True:
            try:
                event = await asyncio.wait_for(
                    self.events.get(), timeout=self.next_poll_in().total_seconds()
                )
                if isinstance(event, StopRunner):
                    return
                if isinstance(event, PollNow):
                    try:
                        polled = await self.poll()
                    except DeviceUnavailable as e:
                        logger.warning("%s: refresh failed: %s", self.device_id, e)
                        continue
                    for item in polled:
                        await self.handle(item)
                    continue
                await self.handle(event)
            except TimeoutError:
                try:
                    events = await self.poll()
                except DeviceUnavailable as e:
                    logger.warning("%s: poll failed: %s", self.device_id, e)
                    events = [Tick()]
                for event in events:
                    await self.handle(event)
