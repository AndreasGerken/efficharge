"""Stand-ins for the parts of ``meross_iot`` the adapter uses (same names and shapes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class OnlineStatus(Enum):
    ONLINE = 1
    OFFLINE = 2


class Namespace(Enum):
    CONTROL_TOGGLEX = "Appliance.Control.ToggleX"
    SYSTEM_ONLINE = "Appliance.System.Online"


# exception class *names* matter: the adapter maps by name, like for the real library
class MissingMFA(Exception):
    pass


class WrongMFA(Exception):
    pass


class BadLoginException(Exception):
    pass


class TooManyTokensException(Exception):
    pass


class TokenExpiredException(Exception):
    pass


class CommandTimeoutError(Exception):
    pass


@dataclass
class PowerInfo:
    power: float


@dataclass
class FakeCreds:
    token: str = "tok-123"
    key: str = "key-abc"
    user_id: str = "u1"
    user_email: str = "rider@example.org"
    domain: str = "https://iotx-eu.meross.com"
    mqtt_domain: str = "mqtt-eu.meross.com"
    issued_on: datetime = field(default_factory=lambda: datetime(2026, 11, 5, tzinfo=UTC))


@dataclass
class FakeClient:
    cloud_credentials: FakeCreds = field(default_factory=FakeCreds)


METERED = {
    "Appliance.Control.ToggleX": {},
    "Appliance.Control.Electricity": {},
    "Appliance.System.All": {},
}


class FakeDevice:
    def __init__(
        self,
        uuid: str,
        name: str,
        abilities: dict | None = None,
        watts: float = 0.0,
        type: str = "mss310",
    ):
        self.uuid = uuid
        self.name = name
        self.type = type  # the model, as in meross_iot
        self.abilities = METERED if abilities is None else abilities
        self.online_status = OnlineStatus.ONLINE
        self.watts = watts
        self._on = True
        self._handlers = []
        self.fail_with: Exception | None = None
        self.updates = 0

    def register_push_notification_handler_coroutine(self, coro) -> None:
        self._handlers.append(coro)

    def is_on(self, channel=0, *args, **kwargs):
        return self._on

    async def async_turn_on(self, channel=0, timeout=None, *args, **kwargs):
        self._check()
        self._on = True

    async def async_turn_off(self, channel=0, timeout=None, *args, **kwargs):
        self._check()
        self._on = False

    async def async_update(self, timeout=None, *args, **kwargs):
        self._check()
        self.updates += 1

    async def async_get_instant_metrics(self, channel=0, timeout=None, *args, **kwargs):
        self._check()
        return PowerInfo(self.watts if self._on else 0.0)

    async def press_button(self) -> None:
        """The physical button: relay toggles, the cloud pushes ToggleX."""
        self._on = not self._on
        for handler in self._handlers:
            await handler(Namespace.CONTROL_TOGGLEX, {"togglex": {"channel": 0}}, self.uuid)

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with


class FakeManager:
    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = devices
        self.closed = False
        self.logged_out = False

    async def async_device_discovery(self, *args, **kwargs):
        return self.devices

    def find_devices(self, *args, **kwargs):
        return list(self.devices)

    def close(self) -> None:
        self.closed = True
