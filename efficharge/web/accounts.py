"""Connecting smart-home accounts: log in at the vendor, pick a plug, start it.

The vendor password is used for exactly one login request and is then dropped. Only the
token the vendor returns is stored, encrypted (see ``efficharge.crypto``).
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from efficharge.crypto import account_aad
from efficharge.db.models import Device, DeviceAccount
from efficharge.db.repo import account_auth, add_account, add_device
from efficharge.demo_setup import add_virtual_plug
from efficharge.devices.base import (
    Capability,
    DeviceError,
    DeviceInfo,
    available_adapters,
    get_adapter_class,
)
from efficharge.devices.meross import LoginError
from efficharge.web import auth, i18n

# app.py imports this module at the end of create_app, when it is fully loaded. The names
# must be module globals: FastAPI resolves the (string) annotations of the routes here.
from efficharge.web.app import DB, Csrf, CurrentUser, render, rt

PICK_CACHE_S = 600
connect_limiter = auth.RateLimiter(limit=10, window_s=15 * 60)


def connectable_kinds() -> dict[str, str]:
    return {k: c.display_name for k, c in available_adapters().items() if k != "simulated"}


def login_error_text(code: str) -> str:
    _ = i18n._
    return {
        "bad_credentials": _("Email or password is wrong."),
        "mfa_required": _(
            "Your account uses two-factor authentication. Please enter the code "
            "from your authenticator app."
        ),
        "mfa_wrong": _("The two-factor code is wrong or expired."),
        "too_many_logins": _(
            "The vendor refused the login: too many logins. Please wait a while and try again."
        ),
    }.get(code, _("Login failed. Please try again later."))


#: the plugs live in the settings page (section "Plugs")
PLUGS_URL = "/settings#plugs"


async def plug_accounts(request: Request, db, user) -> dict:
    """The user's vendor accounts with their plugs, for the settings page."""
    rows = (await db.scalars(select(DeviceAccount).where(DeviceAccount.user_id == user.id))).all()
    devices = (await db.scalars(select(Device).where(Device.user_id == user.id))).all()
    logins = {}
    for a in rows:  # "connected as …": the vendor login email, from the encrypted auth
        try:
            logins[a.id] = account_auth(rt(request).vault, a).get("user_email")
        except Exception:
            logins[a.id] = None
    real = [a for a in rows if a.kind != "simulated"]
    real_ids = {a.id for a in real}
    virtual = [d for d in devices if d.account_id not in real_ids]
    expired = next((a for a in real if a.status != "ok"), None)
    no_plug = next((a for a in real if not any(d.account_id == a.id for d in devices)), None)
    real_plugs = len(devices) - len(virtual)
    # where the user stands, for the card at the top of the settings (the welcome too)
    if expired:
        state = "expired"
    elif real_plugs:
        state = "ready"
    elif real:
        state = "pick"
    elif virtual:
        state = "trying"
    else:
        state = "welcome"
    return {
        "accounts": rows,
        "by_account": {a.id: [d for d in devices if d.account_id == a.id] for a in rows},
        "logins": logins,
        "kinds": connectable_kinds(),
        "has_real_plug_account": bool(real),
        "plug_state": state,
        "real_plugs": real_plugs,
        "virtual_plug": virtual[0] if virtual else None,
        "expired_account": expired,
        "account_without_plug": no_plug,
    }


def register_account_routes(app: FastAPI) -> None:
    def no_demo(request: Request) -> None:
        if request.session.get("demo"):
            raise HTTPException(403, "The demo can't connect real plugs.")

    async def own_account(db, user, account_id: int) -> DeviceAccount:
        account = await db.get(DeviceAccount, account_id)
        if account is None or account.user_id != user.id:
            raise HTTPException(404)
        return account

    @app.get("/accounts")
    async def accounts():
        return RedirectResponse(PLUGS_URL, status_code=303)

    @app.get("/accounts/connect/{kind}", response_class=HTMLResponse)
    async def connect_form(
        request: Request, user: CurrentUser, kind: str, account: int | None = None
    ):
        no_demo(request)
        if kind not in connectable_kinds():
            raise HTTPException(404)
        return render(
            request,
            "connect.html",
            kind=kind,
            vendor=connectable_kinds()[kind],
            error=None,
            need_mfa=False,
            email="",
            reconnect=account,
        )

    @app.post("/accounts/connect/{kind}", dependencies=[Csrf])
    async def connect(
        request: Request,
        db: DB,
        user: CurrentUser,
        kind: str,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        mfa_code: Annotated[str, Form()] = "",
        reconnect: Annotated[int | None, Form()] = None,
    ):
        no_demo(request)
        if kind not in connectable_kinds():
            raise HTTPException(404)
        if not connect_limiter.hit(f"{user.id}"):
            raise HTTPException(429, "too many attempts")
        try:
            stored = await get_adapter_class(kind).login(
                {"email": email, "password": password, "mfa_code": mfa_code}
            )
        except LoginError as e:
            return render(
                request,
                "connect.html",
                400,
                kind=kind,
                vendor=connectable_kinds()[kind],
                error=login_error_text(e.code),
                need_mfa=e.code in ("mfa_required", "mfa_wrong"),
                email=email,
                reconnect=reconnect,
            )
        runtime = rt(request)
        if reconnect is not None:  # expired token: replace it, restart the devices
            account = await own_account(db, user, reconnect)
            if account.kind != kind:
                raise HTTPException(400)
            account.auth_encrypted = runtime.vault.encrypt(stored, account_aad(account.id, kind))
            account.status = "ok"
            await db.commit()
            if runtime.supervisor is not None:
                device_ids = (
                    await db.scalars(
                        select(Device.id).where(
                            Device.account_id == account.id, Device.enabled.is_(True)
                        )
                    )
                ).all()
                await db.commit()  # runners open their own sessions: release the lock
                await runtime.supervisor.remove_account(account.id)
                for device_id in device_ids:
                    await runtime.supervisor.start_device(device_id)
            return RedirectResponse(PLUGS_URL, status_code=303)
        account = await add_account(db, runtime.vault, user, kind, stored)
        await db.commit()
        return RedirectResponse(f"/accounts/{account.id}/pick", status_code=303)

    async def listed_devices(
        request: Request, account: DeviceAccount, refresh: bool = False
    ) -> list[DeviceInfo]:
        """The account's plugs. Cached a few minutes (listing opens a cloud connection),
        but only when something answered: an empty or unreachable result is retried."""
        cache = request.app.state.pick_cache
        hit = cache.get(account.id)
        if hit and not refresh and time.monotonic() - hit[0] < PICK_CACHE_S:
            return hit[1]
        runtime = rt(request)
        session = runtime.supervisor.accounts.get(account.id) if runtime.supervisor else None
        if session is not None:
            infos = await session.adapter.list_devices()
        else:
            adapter = get_adapter_class(account.kind)(account_auth(runtime.vault, account))
            try:
                infos = await adapter.list_devices()
            finally:
                await adapter.close()
        if any(d.online for d in infos):
            cache[account.id] = (time.monotonic(), infos)
        else:
            cache.pop(account.id, None)
        return infos

    @app.get("/accounts/{account_id}/pick", response_class=HTMLResponse)
    async def pick_form(
        request: Request, db: DB, user: CurrentUser, account_id: int, refresh: bool = False
    ):
        no_demo(request)
        account = await own_account(db, user, account_id)
        await db.commit()  # don't hold the DB lock while the vendor cloud answers
        error = None
        infos: list[DeviceInfo] = []
        try:
            infos = await listed_devices(request, account, refresh=refresh)
        except DeviceError:
            error = i18n._("Could not reach your plugs. Please try again in a minute.")
        added = set(
            (
                await db.scalars(
                    select(Device.vendor_device_id).where(Device.account_id == account.id)
                )
            ).all()
        )
        # plugs without power metering can't detect plugging in: shown, but not usable
        devices = [(d, bool(d.capabilities & Capability.POWER_METER)) for d in infos]
        devices.sort(key=lambda dm: not dm[1])  # usable ones first
        return render(
            request,
            "pick.html",
            account=account,
            devices=devices,
            usable=any(metered for _, metered in devices),
            added=added,
            error=error,
        )

    @app.post("/accounts/{account_id}/pick", dependencies=[Csrf])
    async def pick(
        request: Request,
        db: DB,
        user: CurrentUser,
        account_id: int,
        vendor_device_id: Annotated[str, Form()],
        default_hours: Annotated[float, Form()] = 3.0,
    ):
        no_demo(request)
        account = await own_account(db, user, account_id)
        await db.commit()  # don't hold the DB lock while the vendor cloud answers
        info = next(
            (d for d in await listed_devices(request, account) if d.device_id == vendor_device_id),
            None,
        )
        if info is None:
            raise HTTPException(404, "plug not found in this account")
        if not info.capabilities & Capability.POWER_METER:
            raise HTTPException(400, "efficharge needs a plug that measures power")
        exists = await db.scalar(
            select(Device.id).where(
                Device.account_id == account.id, Device.vendor_device_id == vendor_device_id
            )
        )
        if exists:
            return RedirectResponse(f"/devices/{exists}", status_code=303)
        device = await add_device(
            db,
            user,
            info.name[:100],
            account=account,
            vendor_device_id=info.device_id,
            capabilities=info.capabilities,
            default_duration_min=int(min(24, max(0.25, default_hours)) * 60),
        )
        await db.commit()
        if rt(request).supervisor is not None:
            await rt(request).supervisor.start_device(device.id)
        return RedirectResponse(f"/devices/{device.id}/settings?welcome=1", status_code=303)

    @app.post("/accounts/virtual", dependencies=[Csrf])
    async def add_virtual(request: Request, db: DB, user: CurrentUser):
        """Try everything on a simulated plug first (one per account; more over SSH)."""
        no_demo(request)
        existing = await db.scalar(
            select(Device.id)
            .join(DeviceAccount, DeviceAccount.id == Device.account_id)
            .where(Device.user_id == user.id, DeviceAccount.kind == "simulated")
        )
        if existing is None:
            existing = (await add_virtual_plug(db, rt(request).vault, user)).id
            await db.commit()  # it starts when it is opened (demo_setup)
        return RedirectResponse(f"/devices/{existing}", status_code=303)

    @app.post("/accounts/{account_id}/delete", dependencies=[Csrf])
    async def delete_account(request: Request, db: DB, user: CurrentUser, account_id: int):
        no_demo(request)
        account = await own_account(db, user, account_id)
        await db.commit()  # stopping runners needs the DB: release the lock first
        if rt(request).demo is not None:
            await rt(request).demo.forget_account(account.id)  # a virtual plug
        if rt(request).supervisor is not None:
            await rt(request).supervisor.remove_account(account.id)
        request.app.state.pick_cache.pop(account.id, None)
        await db.delete(account)  # devices, cycles, samples go with it (ON DELETE CASCADE)
        await db.commit()
        return RedirectResponse(PLUGS_URL, status_code=303)
