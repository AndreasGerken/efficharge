"""``efficharge meross-check``: read-only diagnosis of a stored Meross account.

1. Lists the account's plugs as the Meross cloud (HTTP API) reports them.
2. Asks each plug over MQTT for its abilities (the step that must work for control).
3. Listens for 60 s: press a plug's button or switch it in the Meross app meanwhile.
   If that push arrives, the way *from* the plug to us works.

Never switches anything and never prints tokens.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from efficharge.crypto import Vault
from efficharge.db.models import DeviceAccount
from efficharge.db.repo import account_auth
from efficharge.db.session import make_engine, make_sessionmaker


async def check(database_url: str, data_keys: str, listen_s: int = 60) -> None:
    from meross_iot.http_api import MerossHttpClient
    from meross_iot.manager import MerossManager
    from meross_iot.model.credentials import MerossCloudCreds
    from meross_iot.model.enums import Namespace

    engine = make_engine(database_url)
    async with make_sessionmaker(engine)() as s:
        accounts = (
            await s.scalars(select(DeviceAccount).where(DeviceAccount.kind == "meross"))
        ).all()
        auths = [(a.id, account_auth(Vault.from_config(data_keys), a)) for a in accounts]
    await engine.dispose()
    if not auths:
        print("No Meross account stored. Connect one in the web app first.")
        return

    for account_id, auth in auths:
        print(f"\n== Meross account #{account_id} ({auth.get('user_email', '?')})")
        client = await MerossHttpClient.async_from_cloud_creds(MerossCloudCreds(**auth))
        devices = await client.async_list_devices()
        print(f"1. Cloud lists {len(devices)} device(s):")
        for d in devices:
            print(
                f"   - {d.dev_name}: {d.device_type}, firmware {d.fmware_version}, "
                f"{d.online_status.name.lower()}, broker {d.get_mqtt_host()}"
            )

        manager = MerossManager(http_client=client, auto_discovery_on_connection=False)
        await manager.async_init()
        print("2. Asking each plug for its abilities (15 s each) …")
        for d in devices:
            started = time.monotonic()
            try:
                answer = await manager.async_execute_cmd(
                    mqtt_hostname=d.get_mqtt_host(),
                    mqtt_port=d.get_mqtt_port(),
                    destination_device_uuid=d.uuid,
                    method="GET",
                    namespace=Namespace.SYSTEM_ABILITY,
                    payload={},
                    timeout=15,
                )
                abilities = (answer or {}).get("ability", {})
                print(
                    f"   ✔ {d.dev_name} answered in {time.monotonic() - started:.1f} s, "
                    f"power metering: {'Appliance.Control.Electricity' in abilities}"
                )
            except Exception as e:
                print(f"   ✘ {d.dev_name}: no answer ({type(e).__name__})")

        received: list[str] = []

        async def on_push(push, target_devices, manager_, devices=devices, received=received):
            # manager-level handler: (notification, target devices, manager)
            uuid = getattr(push, "originating_device_uuid", "?")
            name = next((d.dev_name for d in devices if d.uuid == uuid), uuid)
            ns = getattr(push.namespace, "value", push.namespace)
            online = (push.raw_data or {}).get("online", {}).get("status")
            extra = {1: " (now online)", 2: " (now offline)"}.get(online, "")
            received.append(f"{name}: {ns}")
            print(f"   ⇠ push from {name}: {ns}{extra}")

        manager.register_push_notification_handler_coroutine(on_push)
        print(
            f"3. Listening {listen_s} s. Press a plug's button or switch it in the Meross app now."
        )
        await asyncio.sleep(listen_s)
        print(f"   {len(received)} push message(s) received.")
        manager.close()
        await asyncio.sleep(1)  # let the MQTT thread finish before the event loop closes

    print("\nWhat it means:")
    print(" - 2 ✘ and 3 nothing: the plugs don't talk to the cloud broker we use. Check the plug")
    print("   in the Meross app; if it works there, the plug may use a protocol meross_iot")
    print("   doesn't support (newer firmware).")
    print(" - 2 ✘ but 3 shows pushes: messages from the plug arrive, only our requests don't.")
    print(" - 2 ✔: everything works; reconnect the plug in efficharge.")


def main(database_url: str, data_keys: str) -> None:
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(check(database_url, data_keys))
