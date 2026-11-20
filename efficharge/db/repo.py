"""Write helpers used by the web layer, the CLI and tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from efficharge.crypto import Vault, account_aad
from efficharge.db.models import (
    DeadlineRuleRow,
    Device,
    DeviceAccount,
    DeviceSettings,
    DeviceStateRow,
    User,
    utcnow,
)
from efficharge.devices.base import Capability

WORKDAYS_MASK = 0b0011111

#: Sensible defaults for a new device: workdays → ready after work, evenings → next morning.
DEFAULT_RULES = (
    {
        "weekdays": WORKDAYS_MASK,
        "plugged_from": "06:00",
        "plugged_until": "15:00",
        "ready_by": "17:00",
    },
    {"weekdays": 0b1111111, "plugged_from": "15:00", "plugged_until": "06:00", "ready_by": "07:30"},
)


async def create_user(
    session: AsyncSession,
    email: str,
    password_hash: str | None = None,
    *,
    approved: bool = True,
    **kw: Any,
) -> User:
    """``approved=False``: a registration, waiting for the admin."""
    user = User(
        email=email.strip().lower(),
        password_hash=password_hash,
        approved_at=utcnow() if approved else None,
        **kw,
    )
    session.add(user)
    await session.flush()
    return user


#: registrations waiting for the admin, oldest first
WAITING_REGISTRATIONS = (
    select(User)
    .where(User.approved_at.is_(None), User.is_demo.is_(False))
    .order_by(User.created_at)
)


def accept_registration(user: User) -> None:
    """They can log in now (``efficharge approve`` or /admin)."""
    user.approved_at = utcnow()


async def add_account(
    session: AsyncSession, vault: Vault, user: User, kind: str, auth: dict[str, Any]
) -> DeviceAccount:
    """Store a vendor account. The token is encrypted and bound to the new row's id."""
    account = DeviceAccount(user_id=user.id, kind=kind)
    session.add(account)
    await session.flush()  # we need the id for the associated data
    account.auth_encrypted = vault.encrypt(auth, account_aad(account.id, kind))
    return account


def account_auth(vault: Vault, account: DeviceAccount) -> dict[str, Any]:
    if account.auth_encrypted is None:
        return {}
    return vault.decrypt(account.auth_encrypted, account_aad(account.id, account.kind))


async def add_device(
    session: AsyncSession,
    user: User,
    name: str,
    *,
    account: DeviceAccount | None,
    vendor_device_id: str | None,
    capabilities: Capability,
    default_rules: bool = True,
    **settings: Any,
) -> Device:
    device = Device(
        user_id=user.id,
        account_id=account.id if account else None,
        vendor_device_id=vendor_device_id,
        name=name,
        mode="A" if account else "B",
        capabilities=capabilities.value,
    )
    session.add(device)
    await session.flush()
    metered = bool(capabilities & Capability.POWER_METER)
    session.add(DeviceSettings(device_id=device.id, metered=metered, **settings))
    session.add(DeviceStateRow(device_id=device.id, phase="idle", data={}))
    if default_rules:
        for i, rule in enumerate(DEFAULT_RULES):
            session.add(DeadlineRuleRow(device_id=device.id, position=i, **rule))
    await session.flush()
    return device
