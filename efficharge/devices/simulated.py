"""A fake smart plug with a fake battery, for tests and the public demo.

It implements every capability, so the core is exercised exactly as with real
hardware, and it only uses the ``DeviceAdapter`` interface.
The clock is injectable, so tests and the demo can run faster than real time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from efficharge.devices.base import (
    Capability,
    DeviceAdapter,
    DeviceInfo,
    DeviceUnavailable,
    PowerReading,
    SwitchChanged,
    register,
)

Clock = Callable[[], datetime]
ALL_CAPS = (
    Capability.SWITCH
    | Capability.POWER_METER
    | Capability.SWITCH_STATE
    | Capability.SWITCH_EVENTS
    | Capability.DEVICE_TIMER
)


@dataclass
class SimBattery:
    capacity_wh: float = 500.0
    soc: float = 0.2  # state of charge, 0..1
    max_w: float = 150.0  # charger draw in the constant-current phase
    cv_from: float = 0.8  # taper starts here
    #: like a real charger: it switches off when the taper falls below this share of
    #: its power (a 180 W e-bike charger at ~20 W); the battery is full then
    cutoff_share: float = 0.12

    def power(self) -> float:
        if self.soc >= 0.995:
            return 0.0
        if self.soc < self.cv_from:
            return self.max_w
        taper = self.max_w * (1.0 - self.soc) / (1.0 - self.cv_from)
        if taper < self.cutoff_share * self.max_w:
            self.soc = 1.0  # the charger switched off: full
            return 0.0
        return taper


@dataclass
class SimDevice:
    device_id: str
    name: str
    on: bool = True
    battery: SimBattery | None = None  # None = nothing plugged in
    standby_w: float = 0.4  # charger standby while the battery is full
    auto_on_at: datetime | None = None
    online: bool = True
    updated: datetime | None = None
    energy_wh: float = field(default=0.0)

    def draw(self) -> float:
        if not self.on or self.battery is None:
            return 0.0
        return self.battery.power() or self.standby_w

    def advance(self, now: datetime, step: timedelta = timedelta(seconds=30)) -> list[bool]:
        """Simulate up to ``now``. Returns relay changes caused by the device timer."""
        changes: list[bool] = []
        t = self.updated or now
        while t < now:
            dt = min(step, now - t)
            if self.auto_on_at is not None and not self.on and t >= self.auto_on_at:
                self.on, self.auto_on_at = True, None
                changes.append(True)
            watts = self.draw()
            hours = dt.total_seconds() / 3600
            self.energy_wh += watts * hours
            if self.battery is not None and self.on:
                self.battery.soc = min(
                    1.0, self.battery.soc + watts * hours * 0.9 / self.battery.capacity_wh
                )
            t += dt
        if self.auto_on_at is not None and not self.on and now >= self.auto_on_at:
            self.on, self.auto_on_at = True, None
            changes.append(True)
        self.updated = now
        return changes


@register
class SimulatedAdapter(DeviceAdapter):
    kind: ClassVar[str] = "simulated"
    display_name: ClassVar[str] = "Simulated plug (demo)"
    credential_fields: ClassVar[dict[str, Any]] = {}

    #: Override in tests / the demo to run on accelerated or fixed time.
    clock: Clock = staticmethod(lambda: datetime.now(UTC))

    @classmethod
    async def login(cls, credentials: dict[str, str]) -> dict[str, Any]:
        return {"devices": [{"device_id": "sim-1", "name": "Demo e-bike charger"}]}

    def __init__(self, stored_auth: dict[str, Any], clock: Clock | None = None) -> None:
        super().__init__(stored_auth)
        if clock is not None:
            self.clock = clock
        self.devices: dict[str, SimDevice] = {
            d["device_id"]: SimDevice(d["device_id"], d["name"])
            for d in stored_auth.get("devices", [])
        }

    def _dev(self, device_id: str) -> SimDevice:
        dev = self.devices[device_id]
        if not dev.online:
            raise DeviceUnavailable(f"{device_id} is offline")
        return dev

    async def sync(self) -> None:
        """Advance the simulation and push timer-caused relay changes."""
        now = self.clock()
        for dev in self.devices.values():
            for on in dev.advance(now):
                await self._emit(dev.device_id, SwitchChanged(dev.device_id, on, now))

    async def list_devices(self) -> list[DeviceInfo]:
        return [DeviceInfo(d.device_id, d.name, d.online, ALL_CAPS) for d in self.devices.values()]

    async def turn_on(self, device_id: str) -> None:
        await self.sync()
        dev = self._dev(device_id)
        dev.on, dev.auto_on_at = True, None
        await self._emit(device_id, SwitchChanged(device_id, True, self.clock()))

    async def turn_off(self, device_id: str, auto_on_at: datetime | None = None) -> None:
        await self.sync()
        dev = self._dev(device_id)
        dev.on, dev.auto_on_at = False, auto_on_at
        await self._emit(device_id, SwitchChanged(device_id, False, self.clock()))

    async def get_state(self, device_id: str) -> bool:
        await self.sync()
        return self._dev(device_id).on

    async def get_power(self, device_id: str) -> PowerReading:
        await self.sync()
        return PowerReading(self._dev(device_id).draw(), self.clock())

    # ── simulation controls (tests, demo script) ──

    async def plug_in(self, device_id: str, battery: SimBattery | None = None) -> None:
        await self.sync()
        self.devices[device_id].battery = battery or SimBattery()

    async def unplug(self, device_id: str) -> None:
        await self.sync()
        self.devices[device_id].battery = None

    async def press_button(self, device_id: str) -> None:
        """Toggle the relay like the physical button does (and push the change)."""
        await self.sync()
        dev = self.devices[device_id]
        dev.on = not dev.on
        dev.auto_on_at = None
        await self._emit(device_id, SwitchChanged(device_id, dev.on, self.clock()))
