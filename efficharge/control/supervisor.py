"""Runs one DeviceRunner per enabled device and keeps them alive.

* One ``AccountSession`` (= one adapter instance) per vendor account, shared by the
  account's devices: one Meross cloud connection per account, not per device.
* A crashed runner is restarted with exponential backoff. On restart, the
  machine's ``Startup`` reconciliation runs (when in doubt: plug ON).
* ``AuthenticationFailed`` marks the account ``needs_reauth`` and stops its runners;
  the user has to reconnect the account in the web app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from efficharge.control.machine import Event, Replan
from efficharge.control.planning import build_planner, machine_settings
from efficharge.control.runner import DeviceRunner, StopRunner
from efficharge.control.store import DbStateStore
from efficharge.crypto import Vault
from efficharge.db.models import Device, DeviceAccount, DeviceSettings
from efficharge.db.repo import account_auth
from efficharge.db.session import readers_for
from efficharge.devices.base import (
    AuthenticationFailed,
    Capability,
    DeviceAdapter,
    PowerReading,
    SwitchChanged,
    get_adapter_class,
)

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[str, dict[str, Any]], DeviceAdapter]


def default_adapter_factory(kind: str, auth: dict[str, Any]) -> DeviceAdapter:
    return get_adapter_class(kind)(auth)


#: switching a released plug on: don't let a slow cloud hold up the removal
RELEASE_TIMEOUT_S = 15.0
#: how long a runner may take to finish its current step when asked to stop
STOP_GRACE_S = 3.0


class AccountSession:
    """One adapter for one vendor account; routes pushed events to the right runner."""

    def __init__(self, account_id: int, adapter: DeviceAdapter) -> None:
        self.account_id = account_id
        self.adapter = adapter
        self.runners: dict[str, DeviceRunner] = {}  # by vendor device id
        adapter.set_event_handler(self._dispatch)
        self._connected = False

    async def ensure_connected(self) -> None:
        if not self._connected:
            await self.adapter.connect()
            self._connected = True

    async def _dispatch(self, vendor_id: str, event: SwitchChanged | PowerReading) -> None:
        runner = self.runners.get(vendor_id)
        if runner is not None:
            await runner.on_push(vendor_id, event)

    async def close(self) -> None:
        if self._connected:
            await self.adapter.close()
            self._connected = False


class Supervisor:
    def __init__(
        self,
        sessions: async_sessionmaker,
        vault: Vault,
        *,
        zone: str,
        clock: Callable[[], datetime],
        adapter_factory: AdapterFactory | None = None,
        poll_interval: timedelta = timedelta(seconds=30),
        max_backoff: float = 300.0,
    ) -> None:
        self.sessions = sessions
        self.vault = vault
        self.zone = zone
        self.clock = clock
        self._custom_factory = adapter_factory
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.store = DbStateStore(sessions, zone)
        self.accounts: dict[int, AccountSession] = {}
        self.runners: dict[int, DeviceRunner] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        #: per-device clocks (demo devices with fast-forward); everything else uses ``clock``
        self.device_clocks: dict[int, Callable[[], datetime]] = {}

    def clock_for(self, device_id: int) -> Callable[[], datetime]:
        return self.device_clocks.get(device_id, self.clock)

    def _make_adapter(self, kind: str, auth: dict[str, Any], clock) -> DeviceAdapter:
        """Real adapters by kind; the simulated one runs on its device's clock."""
        if self._custom_factory is not None:
            return self._custom_factory(kind, auth)
        from efficharge.devices.simulated import SimulatedAdapter

        if kind == SimulatedAdapter.kind:
            return SimulatedAdapter(auth, clock=clock)
        return default_adapter_factory(kind, auth)

    # ── lifecycle ──

    async def start(self) -> None:
        async with readers_for(self.sessions)() as session:
            ids = (
                await session.scalars(
                    select(Device.id)
                    .join(DeviceAccount, Device.account_id == DeviceAccount.id)
                    .where(
                        Device.enabled.is_(True),
                        Device.mode == "A",
                        DeviceAccount.status == "ok",
                        # virtual plugs start when they are opened (web.demo_setup)
                        DeviceAccount.kind != "simulated",
                    )
                )
            ).all()
        for device_id in ids:
            await self.start_device(device_id)
        logger.info("supervisor started %d device(s)", len(ids))

    async def stop(self) -> None:
        for device_id in list(self.tasks):
            await self.stop_device(device_id)
        for account in self.accounts.values():
            await account.close()
        self.accounts.clear()

    async def start_device(self, device_id: int) -> None:
        if device_id in self.tasks:
            return
        async with readers_for(self.sessions)() as session:
            device = await session.get(Device, device_id)
            settings = await session.get(DeviceSettings, device_id)
            account = await session.get(DeviceAccount, device.account_id)
            account_session = self.accounts.get(account.id)
            clock = self.clock_for(device_id)
            if account_session is None:
                auth = account_auth(self.vault, account)
                adapter = self._make_adapter(account.kind, auth, clock)
                account_session = AccountSession(account.id, adapter)
                self.accounts[account.id] = account_session

        async def planner():
            async with readers_for(self.sessions)() as s:
                return await build_planner(s, device_id, self.zone, clock())

        runner = DeviceRunner(
            str(device_id),
            account_session.adapter,
            Capability(device.capabilities),
            self.store,
            machine_settings(settings),
            planner,
            clock,
            poll_interval=self.poll_interval,
            vendor_id=device.vendor_device_id,
        )
        account_session.runners[device.vendor_device_id] = runner
        self.runners[device_id] = runner
        self.tasks[device_id] = asyncio.create_task(
            self._supervise(device_id, account_session, runner), name=f"device-{device_id}"
        )

    async def stop_device(self, device_id: int) -> None:
        task = self.tasks.pop(device_id, None)
        runner = self.runners.pop(device_id, None)
        if task is not None and runner is not None and not task.done():
            # ask first: the runner ends after its current step, never mid-write
            runner.send(StopRunner())
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), STOP_GRACE_S)
        if task is not None and not task.done():
            task.cancel()  # stuck (e.g. waiting out a backoff): cancel as before
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if runner is not None:
            for account in self.accounts.values():
                if account.runners.get(runner.vendor_id) is runner:
                    del account.runners[runner.vendor_id]

    async def release_device(self, device_id: int) -> bool:
        """The plug leaves efficharge (removed, or its account): stop controlling it, then
        switch it on. A plug left off while it waited for a window would never charge the
        battery again (the same fail-safe as everywhere: better charged inefficiently).
        Returns whether the plug is on now (False: tell the user to switch it on)."""
        runner = self.runners.get(device_id)
        await self.stop_device(device_id)  # first: so nothing switches it off again
        if runner is None:
            return False  # not running (e.g. its account needs a new login): can't switch it
        try:
            await asyncio.wait_for(runner.adapter.turn_on(runner.vendor_id), RELEASE_TIMEOUT_S)
        except Exception as e:  # best effort: the plug may be offline or the token gone
            logger.warning("%s: couldn't switch the plug on when releasing it: %s", device_id, e)
            return False
        return True

    async def remove_account(self, account_id: int) -> None:
        """Release all plugs of an account and close its vendor session (account deleted)."""
        account = self.accounts.pop(account_id, None)
        if account is None:
            return
        for runner in list(account.runners.values()):
            await self.release_device(int(runner.device_id))
        await account.close()

    async def reload_device(self, device_id: int) -> None:
        """Settings changed in the UI: restart the runner with fresh settings."""
        await self.stop_device(device_id)
        await self.start_device(device_id)

    # ── commands ──

    def send(self, device_id: int, event: Event) -> bool:
        runner = self.runners.get(device_id)
        if runner is None:
            return False
        runner.send(event)
        return True

    async def broadcast_replan(self) -> None:
        """New prices: every planned device reconsiders its window."""
        for runner in self.runners.values():
            runner.send(Replan())

    # ── supervision ──

    async def _supervise(self, device_id: int, account: AccountSession, runner: DeviceRunner):
        failures = 0
        while True:
            try:
                await account.ensure_connected()
                runner.state = None  # reload from the DB after a crash
                await runner.run()
                return  # asked to stop (StopRunner)
            except asyncio.CancelledError:
                raise
            except AuthenticationFailed:
                logger.warning("account %s needs reauthentication", account.account_id)
                await self._mark_needs_reauth(account.account_id)
                return
            except Exception:
                failures += 1
                delay = min(self.max_backoff, 2.0 * 2 ** min(failures, 10))
                logger.exception(
                    "device %s crashed (%d), restart in %.0fs", device_id, failures, delay
                )
                await asyncio.sleep(delay)

    async def _mark_needs_reauth(self, account_id: int) -> None:
        async with self.sessions() as session, session.begin():
            account = await session.get(DeviceAccount, account_id)
            if account is not None:
                account.status = "needs_reauth"
        # TODO(slice 4): notify the user by email (translated).
