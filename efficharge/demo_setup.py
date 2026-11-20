"""Virtual plugs: a simulated plug and battery with real prices, on their own clock.

Every account starts with one (to try everything before connecting a real plug), and
the public demo gives every visitor their own, with a temporary demo user. A virtual
plug is an ordinary device on a "simulated" plug account; only its simulation lives in
memory. It starts when it is opened and stops after two hours without a visit, so an
idle one costs nothing; a demo user is deleted then, an account's virtual plug starts
over the next time.

* Starts in real time. "Fast-forward" runs the visitor's clock 1200× faster, smoothly,
  and stops by itself where the known prices end (beyond that nothing can be planned).
* "Back to now" resets the demo: clock to the real time, no battery, no history.
* A full battery is taken off after a while, like a real rider would.
* At most ``max_demos`` public demos exist at once (memory).

Only virtual plugs use these clocks; real devices always run on real time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from efficharge.control.machine import Tick
from efficharge.control.supervisor import Supervisor
from efficharge.crypto import Vault
from efficharge.db.models import (
    Cycle,
    Device,
    DeviceAccount,
    DeviceStateRow,
    PowerSampleRow,
    User,
)
from efficharge.db.repo import add_account, add_device, create_user
from efficharge.devices.simulated import ALL_CAPS, SimBattery, SimDevice, SimulatedAdapter
from efficharge.energy.store import (
    known_price_until,
)

logger = logging.getLogger(__name__)

DEMO_VENDOR_ID = "sim-1"
DEMO_DOMAIN = "demo.efficharge.invalid"
FAST = 1200.0
DEMO_POLL_S = 2.0  # demo plugs are polled every 2 real seconds (at least)
VIRTUAL_NAMES = {"de": "Virtuelle Steckdose", "en": "Virtual plug"}


async def add_virtual_plug(session: AsyncSession, vault: Vault, user: User) -> Device:
    """A virtual plug for ``user``: its own simulated plug account, so it can be removed
    like any plug (and never mixes with a real account's plugs)."""
    auth = await SimulatedAdapter.login({})
    account = await add_account(session, vault, user, SimulatedAdapter.kind, auth)
    return await add_device(
        session,
        user,
        VIRTUAL_NAMES.get(user.locale, VIRTUAL_NAMES["en"]),
        account=account,
        vendor_device_id=DEMO_VENDOR_ID,
        capabilities=ALL_CAPS,
        battery_kwh=0.5,
        # Show planned windows rather than "no time to wait": no workday rules (the
        # default rule: ready within 12 h). The estimate matches the simulated battery
        # (500 Wh from 25 % at 150 W): about 3.5 h.
        default_rules=False,
        default_duration_min=210,
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


class DemoClock:
    """A clock that can run faster than real time without jumping.

    Changing the speed rebases it at the current simulated time, so the time line is
    continuous. ``limit`` caps fast-forward: once reached, it continues at 1×.
    """

    def __init__(self, real: Callable[[], datetime] = utc_now) -> None:
        self._real = real
        self.limit: datetime | None = None
        self._rebase(real(), 1.0)

    def _rebase(self, at: datetime, speed: float) -> None:
        self._sim0, self._mono0, self.speed = at, time.monotonic(), speed

    def __call__(self) -> datetime:
        now = self._sim0 + timedelta(seconds=(time.monotonic() - self._mono0) * self.speed)
        if self.speed > 1 and self.limit is not None and now >= self.limit:
            self._rebase(self.limit, 1.0)
            return self.limit
        return now

    def set_speed(self, speed: float, limit: datetime | None = None) -> None:
        current = self()
        self.limit = limit
        self._rebase(current, speed)

    def reset(self) -> None:
        self.limit = None
        self._rebase(self._real(), 1.0)

    def real_now(self) -> datetime:
        return self._real()


@dataclass
class DemoInstance:
    user_id: int
    account_id: int
    device_id: int
    clock: DemoClock
    #: a public demo: its demo user is deleted with it
    temporary: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    full_since: datetime | None = None
    polling_fast: bool = False
    #: fast-forward started by the plug-in itself: ends when the battery comes off
    auto_fast: bool = False


class DemoFull(Exception):
    """Too many demos at once."""


class DemoManager:
    def __init__(
        self,
        sessions: async_sessionmaker,
        vault: Vault,
        supervisor: Supervisor,
        *,
        zone: str,
        max_demos: int = 50,
        idle_timeout: timedelta = timedelta(hours=2),
        public_idle_timeout: timedelta = timedelta(minutes=15),
        take_off_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.sessions = sessions
        self.vault = vault
        self.supervisor = supervisor
        self.zone = zone
        self.max_demos = max_demos
        self.idle_timeout = idle_timeout
        #: a public demo's page asks for its status every few seconds while it is open:
        #: 15 min without a request means the visitor left (or it was a bot): free the slot
        self.public_idle_timeout = public_idle_timeout
        self.take_off_after = take_off_after
        self.demos: dict[int, DemoInstance] = {}  # by device id
        self._lock = asyncio.Lock()

    # ── lifecycle ──

    async def create(self, locale: str = "en") -> DemoInstance:
        """A public demo: a temporary demo user with a virtual plug."""
        async with self._lock:
            if sum(d.temporary for d in self.demos.values()) >= self.max_demos:
                raise DemoFull
            async with self.sessions() as s, s.begin():
                user = await create_user(
                    s, f"{secrets.token_hex(8)}@{DEMO_DOMAIN}", is_demo=True, locale=locale
                )
                device = await add_virtual_plug(s, self.vault, user)
            demo = DemoInstance(user.id, device.account_id, device.id, DemoClock(), temporary=True)
            self.demos[device.id] = demo
            self.supervisor.device_clocks[device.id] = demo.clock
            await self.supervisor.start_device(device.id)
            self._fit_polling(demo)
            return demo

    async def open(self, device_id: int) -> DemoInstance | None:
        """The simulation of a virtual plug, started if it isn't running (it starts over:
        no battery, no history). None for a real plug."""
        if (demo := self.get(device_id)) is not None:
            return demo
        async with self._lock:
            if (demo := self.get(device_id)) is not None:
                return demo
            async with self.sessions() as s:
                device = await s.get(Device, device_id)
                account = await s.get(DeviceAccount, device.account_id) if device else None
            if account is None or account.kind != SimulatedAdapter.kind:
                return None
            demo = DemoInstance(device.user_id, account.id, device.id, DemoClock())
            self.demos[device.id] = demo
            self.supervisor.device_clocks[device.id] = demo.clock
            await self.reset(demo)
            return demo

    def get(self, device_id: int | None) -> DemoInstance | None:
        demo = self.demos.get(device_id) if device_id is not None else None
        if demo is not None:
            demo.last_seen = time.monotonic()
        return demo

    async def delete(self, demo: DemoInstance) -> None:
        """Stop the simulation. A public demo's user goes with it (and everything of it)."""
        self.demos.pop(demo.device_id, None)
        await self.supervisor.remove_account(demo.account_id)
        self.supervisor.device_clocks.pop(demo.device_id, None)
        if not demo.temporary:
            return
        async with self.sessions() as s, s.begin():
            user = await s.get(User, demo.user_id)
            if user is not None:
                await s.delete(user)  # cascades to account, device, cycles, samples

    async def forget_account(self, account_id: int) -> None:
        """The user removed a virtual plug's account: stop its simulation."""
        for demo in [d for d in self.demos.values() if d.account_id == account_id]:
            await self.delete(demo)

    async def delete_leftovers(self) -> None:
        """Demo users from a previous run (their in-memory plug is gone): remove them."""
        async with self.sessions() as s, s.begin():
            for user in (await s.scalars(select(User).where(User.is_demo.is_(True)))).all():
                await s.delete(user)

    # ── controls ──

    def adapter(self, demo: DemoInstance) -> SimulatedAdapter | None:
        account = self.supervisor.accounts.get(demo.account_id)
        if account is not None and isinstance(account.adapter, SimulatedAdapter):
            return account.adapter
        return None

    def sim_device(self, demo: DemoInstance) -> SimDevice | None:
        adapter = self.adapter(demo)
        return adapter.devices.get(DEMO_VENDOR_ID) if adapter else None

    async def plug_in(self, demo: DemoInstance, fast: bool = True) -> None:
        """Plug a battery in. A virtual charge takes hours, so the clock fast-forwards by
        itself (unless it already runs fast) until the battery is taken off again."""
        if adapter := self.adapter(demo):
            await adapter.plug_in(DEMO_VENDOR_ID, SimBattery(soc=0.25))
            if fast and demo.clock.speed == 1:
                await self.set_speed(demo, FAST)
                demo.auto_fast = True

    async def unplug(self, demo: DemoInstance) -> None:
        if adapter := self.adapter(demo):
            await adapter.unplug(DEMO_VENDOR_ID)
        demo.full_since = None
        if demo.auto_fast:  # the charge is over: back to normal speed
            await self.set_speed(demo, 1.0)

    async def press_button(self, demo: DemoInstance) -> None:
        if adapter := self.adapter(demo):
            await adapter.press_button(DEMO_VENDOR_ID)

    async def set_speed(self, demo: DemoInstance, speed: float) -> None:
        demo.auto_fast = False  # a speed chosen by hand stays as it is
        limit = None
        if speed > 1:
            async with self.sessions() as s:
                known = await known_price_until(s, self.zone)
            # beyond the published prices nothing can be planned: stop an hour before
            limit = known - timedelta(minutes=45) if known else None
        if adapter := self.adapter(demo):
            await adapter.sync()  # settle the simulation at the old speed first
        demo.clock.set_speed(speed, limit)
        self._fit_polling(demo)

    def _fit_polling(self, demo: DemoInstance) -> None:
        """Poll often: a simulated plug costs nothing, and a visitor who clicks "plug in"
        should see the reaction within seconds (a real plug is polled every 30 s)."""
        runner = self.supervisor.runners.get(demo.device_id)
        if runner is not None:
            normal = self.supervisor.poll_interval.total_seconds()
            seconds = min(DEMO_POLL_S, normal / demo.clock.speed)
            runner.poll_interval = timedelta(seconds=max(0.1, seconds))
            runner.send(Tick())  # wake it now, not after the old (long) poll interval
        demo.polling_fast = demo.clock.speed > 1

    async def reset(self, demo: DemoInstance) -> None:
        """Back to now: no battery, no history."""
        await self.supervisor.stop_device(demo.device_id)
        demo.clock.reset()
        demo.full_since = None
        demo.auto_fast = False
        if adapter := self.adapter(demo):
            adapter.devices[DEMO_VENDOR_ID] = SimDevice(DEMO_VENDOR_ID, "Virtual plug")
        async with self.sessions() as s, s.begin():
            await s.execute(delete(Cycle).where(Cycle.device_id == demo.device_id))
            await s.execute(
                delete(PowerSampleRow).where(PowerSampleRow.device_id == demo.device_id)
            )
            row = await s.get(DeviceStateRow, demo.device_id)
            row.phase, row.data, row.current_cycle_id = "idle", {}, None
            row.last_power_w = row.last_seen = None
        await self.supervisor.start_device(demo.device_id)
        self._fit_polling(demo)

    # ── background ──

    async def step(self) -> None:
        """Take full batteries off, end fast-forward bookkeeping, delete idle demos."""
        for demo in list(self.demos.values()):
            timeout = self.public_idle_timeout if demo.temporary else self.idle_timeout
            if time.monotonic() - demo.last_seen > timeout.total_seconds():
                logger.info("stopping idle virtual plug %s", demo.device_id)
                await self.delete(demo)
                continue
            if demo.polling_fast and demo.clock.speed == 1:
                self._fit_polling(demo)  # fast-forward hit its limit and fell back to 1×
            adapter = self.adapter(demo)
            dev = self.sim_device(demo)
            if adapter is None or dev is None or dev.battery is None:
                continue
            await adapter.sync()
            if dev.battery.soc < 0.995:
                demo.full_since = None
                continue
            now = demo.clock()
            demo.full_since = demo.full_since or now
            if now - demo.full_since >= self.take_off_after:
                await self.unplug(demo)

    async def run(self, tick_s: float = 0.5) -> None:
        while True:
            try:
                await self.step()
            except Exception:
                logger.exception("demo manager step failed")
            await asyncio.sleep(tick_s)

    async def close(self) -> None:
        for demo in list(self.demos.values()):
            with contextlib.suppress(Exception):
                await self.delete(demo)
