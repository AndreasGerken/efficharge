"""Meross cloud plugs (e.g. MSS305, MSS310) via the ``meross_iot`` library.

* Login exchanges email + password (+ MFA code) for a cloud **token** once. Only the
  token is stored (encrypted). The password is never stored, and we never log out on
  shutdown: a logout would invalidate the stored token.
* One ``MerossManager`` (= one MQTT connection to the Meross cloud) per account.
  Meross bans accounts that log in too often, so we reuse the token on every start.
* The plug's button: Meross pushes ``Appliance.Control.ToggleX`` over MQTT. We turn it
  into a ``SwitchChanged`` event, so a press reaches the state machine within ~1 s.
* No on-plug timer support in the library, so no ``DEVICE_TIMER``: while a plug waits
  in PLANNED, our server must be up to switch it on (the fail-safe on restart turns
  plugs on if their window has passed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, ClassVar

from efficharge.devices.base import (
    AuthenticationFailed,
    Capability,
    DeviceAdapter,
    DeviceError,
    DeviceInfo,
    DeviceUnavailable,
    PowerReading,
    SwitchChanged,
    register,
)

logger = logging.getLogger(__name__)

API_BASE = "https://iotx-eu.meross.com"  # the library retries other regions by itself
NS_TOGGLEX = "Appliance.Control.ToggleX"
NS_TOGGLE = "Appliance.Control.Toggle"
NS_ELECTRICITY = "Appliance.Control.Electricity"
#: plugs with power metering, used for plugs that didn't report their abilities
METERED_MODELS = {"mss305", "mss310", "mss315"}
FULL_UPDATE_EVERY_S = 300  # re-read the relay state from the plug if pushes were missed
#: a plug that is missing or offline: connect and discover again at most this often. The
#: library keeps the device list of its first discovery; a plug that was offline then (or
#: went offline later) would otherwise stay "offline" until the app restarts.
REDISCOVER_EVERY_S = 300
COMMAND_TIMEOUT_S = 10.0


def is_plug(model: str | None) -> bool:
    """A plug or power strip (MSS…, outdoor MOP…), not a lamp (MSL), hub (MSH), garage
    opener (MSG), thermostat (MTS) or in-wall switch (MSS5xx): only plugs are listed."""
    model = (model or "").lower()
    return model.startswith(("mss", "mop")) and not model.startswith("mss5")


class LoginError(DeviceError):
    """Login refused. ``code`` is translated by the web layer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code  # bad_credentials | mfa_required | mfa_wrong | too_many_logins | unknown


def _meross():
    """Import lazily: the library is big and only needed when a Meross account exists."""
    from meross_iot.http_api import MerossHttpClient
    from meross_iot.manager import MerossManager
    from meross_iot.model.credentials import MerossCloudCreds

    return MerossHttpClient, MerossManager, MerossCloudCreds


def _map_error(e: Exception) -> DeviceError:
    """meross_iot exceptions → our two kinds: retry later, or the user must reconnect."""
    name = type(e).__name__
    if name in ("TokenExpiredException", "UnauthorizedException", "BadLoginException"):
        return AuthenticationFailed(name)
    return DeviceUnavailable(f"{name}: {e}")


@register
class MerossAdapter(DeviceAdapter):
    kind: ClassVar[str] = "meross"
    display_name: ClassVar[str] = "Meross"
    credential_fields: ClassVar[dict[str, Any]] = {
        "email": "email",
        "password": "password",
        "mfa_code": "mfa",  # optional, only for accounts with two-factor authentication
    }

    #: test seams: replace with fakes (see tests/test_meross.py)
    client_from_password: ClassVar[Callable | None] = None
    connect_from_creds: ClassVar[Callable | None] = None

    @classmethod
    async def login(cls, credentials: dict[str, str]) -> dict[str, Any]:
        email = credentials.get("email", "").strip()
        password = credentials.get("password", "")
        mfa = (credentials.get("mfa_code") or "").strip() or None
        if not email or not password:
            raise LoginError("bad_credentials")
        try:
            if cls.client_from_password is not None:
                client = await cls.client_from_password(email, password, mfa)
            else:
                http_client_cls, _, _ = _meross()
                client = await http_client_cls.async_from_user_password(
                    api_base_url=API_BASE, email=email, password=password, mfa_code=mfa
                )
        except Exception as e:
            name = type(e).__name__
            code = {
                "MissingMFA": "mfa_required",
                "WrongMFA": "mfa_wrong",
                "BadLoginException": "bad_credentials",
                "TooManyTokensException": "too_many_logins",
            }.get(name, "unknown")
            logger.info("Meross login failed: %s", name)  # never log the credentials
            raise LoginError(code) from e
        creds = client.cloud_credentials
        return {
            "token": creds.token,
            "key": creds.key,
            "user_id": creds.user_id,
            "user_email": creds.user_email,
            "domain": creds.domain,
            "mqtt_domain": creds.mqtt_domain,
            "issued_on": creds.issued_on.isoformat(),
        }

    def __init__(self, stored_auth: dict[str, Any]) -> None:
        super().__init__(stored_auth)
        self._manager = None
        self._devices: dict[str, Any] = {}
        #: the account's devices as the HTTP API lists them, also those that didn't answer
        self._http_devices: list[Any] = []
        self._last_full_update: dict[str, float] = {}
        self._discovered_at: float | None = None  # time.monotonic() of the last discovery
        self._lock = asyncio.Lock()

    # ── connection ──

    async def connect(self, *, again: bool = False) -> None:
        """Log in with the stored token and discover the account's plugs (once).
        ``again``: drop the connection and discover anew (a plug came back)."""
        async with self._lock:
            if again:
                self._disconnect()
            if self._manager is not None:
                return
            self._discovered_at = time.monotonic()
            try:
                if self.connect_from_creds is not None:
                    self._manager = await self.connect_from_creds(self.stored_auth)
                else:
                    self._manager = await self._connect_real()
                await self._manager.async_device_discovery()
            except Exception as e:
                self._manager = None
                raise _map_error(e) from e
            for device in self._manager.find_devices():
                self._attach(device)

    async def _connect_real(self):
        http_client_cls, manager_cls, creds_cls = _meross()
        creds = creds_cls(**self.stored_auth)
        client = await http_client_cls.async_from_cloud_creds(creds)
        # no automatic discovery on connect: connect() runs it once itself
        manager = manager_cls(http_client=client, auto_discovery_on_connection=False)
        await manager.async_init()
        self._http_devices = await client.async_list_devices()
        return manager

    async def close(self) -> None:
        self._disconnect()

    def _disconnect(self) -> None:
        # Close MQTT, but do NOT log out: that would invalidate the stored token.
        if self._manager is not None:
            self._manager.close()
            self._manager = None
            self._devices.clear()
            self._last_full_update.clear()

    def _attach(self, device) -> None:
        uuid = device.uuid
        if uuid in self._devices:
            return
        self._devices[uuid] = device

        async def on_push(namespace, data, device_internal_id, *args, **kwargs):
            ns = getattr(namespace, "value", namespace)
            if ns in (NS_TOGGLEX, NS_TOGGLE):
                on = device.is_on(channel=0)
                if on is not None:
                    await self._emit(uuid, SwitchChanged(uuid, bool(on)))

        device.register_push_notification_handler_coroutine(on_push)

    async def _device(self, device_id: str):
        await self.connect()
        device = self._devices.get(device_id)
        if _status(device) != "ONLINE" and self._may_rediscover():
            await self.connect(again=True)
            device = self._devices.get(device_id)
        if device is None:
            raise DeviceUnavailable(f"Meross device {device_id} not found in this account")
        if (status := _status(device)) != "ONLINE":
            raise DeviceUnavailable(f"Meross device {device_id} is {status.lower()}")
        return device

    def _may_rediscover(self) -> bool:
        last = self._discovered_at
        return last is None or time.monotonic() - last >= REDISCOVER_EVERY_S

    # ── DeviceAdapter ──

    async def list_devices(self) -> list[DeviceInfo]:
        await self.connect()
        out = []
        for uuid, device in self._devices.items():
            abilities = getattr(device, "abilities", {}) or {}
            if not is_plug(getattr(device, "type", None)):
                continue  # a lamp, hub, sensor, …
            if NS_TOGGLEX not in abilities and NS_TOGGLE not in abilities:
                continue  # can't be switched
            caps = Capability.SWITCH | Capability.SWITCH_STATE | Capability.SWITCH_EVENTS
            if NS_ELECTRICITY in abilities:
                caps |= Capability.POWER_METER
            online = _status(device) == "ONLINE"
            out.append(DeviceInfo(uuid, device.name, online, caps))
        # Listed by the cloud but not enrolled (didn't answer the ability query): show them
        # as unreachable instead of hiding them, so the user sees what's going on.
        for info in self._http_devices:
            if info.uuid in self._devices or not is_plug(getattr(info, "device_type", None)):
                continue
            caps = Capability.SWITCH | Capability.SWITCH_STATE | Capability.SWITCH_EVENTS
            if str(getattr(info, "device_type", "")).lower() in METERED_MODELS:
                caps |= Capability.POWER_METER
            out.append(DeviceInfo(info.uuid, info.dev_name, False, caps))
        return out

    async def turn_on(self, device_id: str) -> None:
        device = await self._device(device_id)
        try:
            await device.async_turn_on(channel=0, timeout=COMMAND_TIMEOUT_S)
        except Exception as e:
            raise _map_error(e) from e

    async def turn_off(self, device_id: str, auto_on_at=None) -> None:
        device = await self._device(device_id)  # auto_on_at: no on-plug timers (see module doc)
        try:
            await device.async_turn_off(channel=0, timeout=COMMAND_TIMEOUT_S)
        except Exception as e:
            raise _map_error(e) from e

    async def get_state(self, device_id: str) -> bool:
        device = await self._device(device_id)
        try:
            last = self._last_full_update.get(device_id)  # None: never (monotonic starts at boot)
            stale = last is None or time.monotonic() - last > FULL_UPDATE_EVERY_S
            if stale:  # pushes may have been missed (MQTT reconnect): ask the plug itself
                await device.async_update(timeout=COMMAND_TIMEOUT_S)
                self._last_full_update[device_id] = time.monotonic()
            on = device.is_on(channel=0)
        except Exception as e:
            raise _map_error(e) from e
        if on is None:
            raise DeviceUnavailable("relay state unknown")
        return bool(on)

    async def get_power(self, device_id: str) -> PowerReading:
        device = await self._device(device_id)
        try:
            info = await device.async_get_instant_metrics(channel=0, timeout=COMMAND_TIMEOUT_S)
        except Exception as e:
            raise _map_error(e) from e
        return PowerReading(float(info.power))


def _status(device) -> str | None:
    """ "ONLINE", "OFFLINE", … of a discovered device; None if it wasn't discovered."""
    if device is None:
        return None
    return getattr(device.online_status, "name", str(device.online_status))
