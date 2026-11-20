"""Device abstraction: the only thing the charging logic knows about hardware.

Every smart-home integration (Meross, Shelly, Tasmota/MQTT, Home Assistant, ...)
implements ``DeviceAdapter``. The planner and the per-device state machine only
use this interface, so adding a new vendor means adding a single module here.

One adapter instance belongs to one *user account* at a vendor and can control
several devices of that account.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Flag, auto
from typing import Any, ClassVar


class Capability(Flag):
    SWITCH = auto()  # can turn power on/off
    POWER_METER = auto()  # can report current power in W (needed for auto plug-in/full detection)
    ENERGY_METER = auto()  # can report cumulative energy in kWh (nice for stats)
    SWITCH_STATE = auto()  # can report whether the relay is on (get_state)
    SWITCH_EVENTS = auto()  # pushes relay changes, e.g. a press of the plug's button
    DEVICE_TIMER = auto()  # can switch itself back on at a given time (dead-man's switch)


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str  # vendor-specific stable id
    name: str
    online: bool
    capabilities: Capability


@dataclass(frozen=True)
class PowerReading:
    watts: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class SwitchChanged:
    device_id: str
    on: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


#: Callback an adapter calls for pushed events (switch changes, power readings).
EventHandler = Callable[[str, "SwitchChanged | PowerReading"], Awaitable[None]]


class DeviceError(Exception):
    """Base error for adapter failures."""


class DeviceUnavailable(DeviceError):
    """Temporary problem (timeout, device offline). The caller should retry later."""


class AuthenticationFailed(DeviceError):
    """Credentials or token are invalid. The user must reconnect the account."""


class DeviceAdapter(ABC):
    #: unique key stored in the DB, e.g. "meross", "shelly_cloud", "mqtt", "home_assistant"
    kind: ClassVar[str]
    #: human-readable name for the web UI
    display_name: ClassVar[str]
    #: describes the fields the "connect account" form must ask for,
    #: e.g. {"email": "text", "password": "password", "region": ["eu", "us", "ap"]}
    credential_fields: ClassVar[dict[str, Any]]

    @classmethod
    @abstractmethod
    async def login(cls, credentials: dict[str, str]) -> dict[str, Any]:
        """Exchange user-entered credentials for something storable (usually a token).

        The returned dict is stored encrypted in the DB and passed to ``__init__``.
        Passwords should NOT be part of it if the vendor offers tokens.
        """

    def __init__(self, stored_auth: dict[str, Any]) -> None:
        self.stored_auth = stored_auth
        self._event_handler: EventHandler | None = None

    def set_event_handler(self, handler: EventHandler | None) -> None:
        """Register the callback for pushed events (``Capability.SWITCH_EVENTS``)."""
        self._event_handler = handler

    async def _emit(self, device_id: str, event: SwitchChanged | PowerReading) -> None:
        if self._event_handler is not None:
            await self._event_handler(device_id, event)

    async def connect(self) -> None:  # noqa: B027 - optional hook
        """Open long-lived connections (MQTT, websocket, ...). Optional."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections. Optional."""

    @abstractmethod
    async def list_devices(self) -> list[DeviceInfo]:
        """All devices of this account, so the user can pick one in the UI."""

    @abstractmethod
    async def turn_on(self, device_id: str) -> None: ...

    @abstractmethod
    async def turn_off(self, device_id: str, auto_on_at: datetime | None = None) -> None:
        """Switch off. With ``Capability.DEVICE_TIMER`` and ``auto_on_at``, the plug
        also schedules switching itself back on at that time, so charging still starts
        if our server or the vendor cloud is down. Others ignore ``auto_on_at``."""

    async def get_state(self, device_id: str) -> bool:
        """Relay on/off. Only for adapters with ``Capability.SWITCH_STATE``."""
        raise NotImplementedError(f"{self.kind} cannot report the switch state")

    async def get_power(self, device_id: str) -> PowerReading:
        """Current power draw. Only for adapters with ``Capability.POWER_METER``."""
        raise NotImplementedError(f"{self.kind} does not support power metering")


_REGISTRY: dict[str, type[DeviceAdapter]] = {}


def register(cls: type[DeviceAdapter]) -> type[DeviceAdapter]:
    """Class decorator: ``@register`` makes an adapter selectable in the UI."""
    if cls.kind in _REGISTRY:
        raise ValueError(f"Adapter kind {cls.kind!r} registered twice")
    _REGISTRY[cls.kind] = cls
    return cls


def get_adapter_class(kind: str) -> type[DeviceAdapter]:
    return _REGISTRY[kind]


def available_adapters() -> dict[str, type[DeviceAdapter]]:
    return dict(_REGISTRY)
