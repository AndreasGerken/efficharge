"""Meross adapter against fakes of the meross_iot objects."""

from __future__ import annotations

import pytest

from efficharge.devices.base import (
    AuthenticationFailed,
    Capability,
    DeviceUnavailable,
    SwitchChanged,
)
from efficharge.devices.meross import LoginError, MerossAdapter
from tests import fake_meross as fm


@pytest.fixture
def plugs(monkeypatch):
    devices = [
        fm.FakeDevice("uuid-bike", "E-Bike Ladegerät", watts=120.0),
        fm.FakeDevice(
            "uuid-old", "Alte Steckdose", abilities={"Appliance.Control.ToggleX": {}}, type="mss110"
        ),
        fm.FakeDevice("uuid-sensor", "Sensor", abilities={"Appliance.System.All": {}}),
        # switchable, but not plugs
        fm.FakeDevice("uuid-lamp", "Lampe", abilities=fm.METERED, type="msl120"),
        fm.FakeDevice("uuid-wall", "Flur", abilities=fm.METERED, type="mss510"),
    ]
    managers = []

    async def connect(stored_auth):
        assert stored_auth["token"] == "tok-123"  # connects with the stored token only
        managers.append(fm.FakeManager(devices))
        return managers[-1]

    monkeypatch.setattr(MerossAdapter, "connect_from_creds", staticmethod(connect))
    return devices, managers


async def test_login_stores_token_not_password(monkeypatch):
    seen = {}

    async def client_from_password(email, password, mfa):
        seen.update(email=email, mfa=mfa)
        return fm.FakeClient()

    monkeypatch.setattr(MerossAdapter, "client_from_password", staticmethod(client_from_password))
    stored = await MerossAdapter.login(
        {"email": " rider@example.org ", "password": "secret!", "mfa_code": ""}
    )
    assert stored["token"] == "tok-123"
    assert "secret!" not in repr(stored)
    assert "password" not in stored
    assert seen == {"email": "rider@example.org", "mfa": None}


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (fm.MissingMFA(), "mfa_required"),
        (fm.WrongMFA(), "mfa_wrong"),
        (fm.BadLoginException(), "bad_credentials"),
        (fm.TooManyTokensException(), "too_many_logins"),
        (RuntimeError("network"), "unknown"),
    ],
)
async def test_login_errors_become_codes(monkeypatch, exc, code):
    async def fail(*args):
        raise exc

    monkeypatch.setattr(MerossAdapter, "client_from_password", staticmethod(fail))
    with pytest.raises(LoginError) as info:
        await MerossAdapter.login({"email": "a@b.c", "password": "x"})
    assert info.value.code == code


async def test_list_devices_only_switchable_plugs_with_capabilities(plugs):
    adapter = MerossAdapter({"token": "tok-123"})
    infos = {d.device_id: d for d in await adapter.list_devices()}
    # no sensor (can't switch), lamp or wall switch (not plugs)
    assert set(infos) == {"uuid-bike", "uuid-old"}
    assert infos["uuid-bike"].capabilities & Capability.POWER_METER
    assert not infos["uuid-old"].capabilities & Capability.POWER_METER
    assert infos["uuid-bike"].capabilities & Capability.SWITCH_EVENTS
    assert not infos["uuid-bike"].capabilities & Capability.DEVICE_TIMER  # not supported


async def test_unreachable_devices_are_listed_only_if_they_are_plugs(plugs):
    from types import SimpleNamespace as Http

    adapter = MerossAdapter({"token": "tok-123"})
    await adapter.connect()
    adapter._http_devices = [  # listed by the cloud, but didn't answer the ability query
        Http(uuid="uuid-off", dev_name="Keller", device_type="mss315"),
        Http(uuid="uuid-off-old", dev_name="Garten", device_type="mss110"),
        Http(uuid="uuid-off-lamp", dev_name="Licht", device_type="msl430"),
        Http(uuid="uuid-off-hub", dev_name="Hub", device_type="msh300"),
    ]
    infos = {d.device_id: d for d in await adapter.list_devices()}
    assert {"uuid-off", "uuid-off-old"} <= set(infos)
    assert "uuid-off-lamp" not in infos and "uuid-off-hub" not in infos
    assert not infos["uuid-off"].online
    assert infos["uuid-off"].capabilities & Capability.POWER_METER  # known model
    assert not infos["uuid-off-old"].capabilities & Capability.POWER_METER


async def test_switch_power_state_and_button_push(plugs):
    devices, managers = plugs
    adapter = MerossAdapter({"token": "tok-123"})
    events = []

    async def handler(device_id, event):
        events.append((device_id, event))

    adapter.set_event_handler(handler)
    await adapter.turn_off("uuid-bike")
    assert await adapter.get_state("uuid-bike") is False
    assert (await adapter.get_power("uuid-bike")).watts == 0.0
    await adapter.turn_on("uuid-bike")
    assert (await adapter.get_power("uuid-bike")).watts == 120.0

    await devices[0].press_button()  # the physical button → ToggleX push
    assert len(events) == 1
    device_id, event = events[0]
    assert device_id == "uuid-bike"
    assert isinstance(event, SwitchChanged) and event.on is False

    await adapter.close()
    assert managers[0].closed
    assert len(managers) == 1  # one connection for the whole account


async def test_state_is_refreshed_from_the_plug_now_and_then(plugs):
    devices, _ = plugs
    adapter = MerossAdapter({"token": "tok-123"})
    await adapter.get_state("uuid-bike")
    await adapter.get_state("uuid-bike")
    assert devices[0].updates == 1  # full update once, then pushes keep it current


async def test_errors_map_to_retry_or_reconnect(plugs):
    devices, _ = plugs
    adapter = MerossAdapter({"token": "tok-123"})
    devices[0].online_status = fm.OnlineStatus.OFFLINE
    with pytest.raises(DeviceUnavailable):
        await adapter.turn_on("uuid-bike")
    devices[0].online_status = fm.OnlineStatus.ONLINE
    devices[0].fail_with = fm.CommandTimeoutError()
    with pytest.raises(DeviceUnavailable):
        await adapter.get_power("uuid-bike")
    devices[0].fail_with = fm.TokenExpiredException()
    with pytest.raises(AuthenticationFailed):
        await adapter.turn_off("uuid-bike")
    with pytest.raises(DeviceUnavailable):
        await adapter.get_power("unknown-uuid")


async def test_connection_failure_with_expired_token(monkeypatch):
    async def connect(stored_auth):
        raise fm.TokenExpiredException()

    monkeypatch.setattr(MerossAdapter, "connect_from_creds", staticmethod(connect))
    with pytest.raises(AuthenticationFailed):
        await MerossAdapter({"token": "old"}).list_devices()


async def test_first_state_is_a_full_update_even_right_after_boot(plugs, monkeypatch):
    devices, _ = plugs
    monkeypatch.setattr("efficharge.devices.meross.time.monotonic", lambda: 5.0)  # just booted
    adapter = MerossAdapter({"token": "tok-123"})
    await adapter.get_state("uuid-bike")
    assert devices[0].updates == 1


async def test_a_plug_that_comes_back_is_found_again(plugs, monkeypatch):
    """The library keeps its first discovery: a plug offline then (or missing from it)
    is looked up again, at most every REDISCOVER_EVERY_S."""
    from efficharge.devices import meross

    devices, managers = plugs
    clock = [1000.0]
    monkeypatch.setattr("efficharge.devices.meross.time.monotonic", lambda: clock[0])
    bike = devices.pop(0)  # offline at boot: the cloud didn't enroll it
    adapter = MerossAdapter({"token": "tok-123"})
    with pytest.raises(DeviceUnavailable, match="not found"):
        await adapter.get_power("uuid-bike")
    devices.insert(0, bike)  # back online
    with pytest.raises(DeviceUnavailable):  # too soon: no new discovery yet
        await adapter.get_power("uuid-bike")
    assert len(managers) == 1
    clock[0] += meross.REDISCOVER_EVERY_S
    assert (await adapter.get_power("uuid-bike")).watts == 120.0
    assert len(managers) == 2 and managers[0].closed and not managers[0].logged_out
    await adapter.get_power("uuid-bike")
    assert len(managers) == 2  # online: the connection stays
