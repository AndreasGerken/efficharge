"""Users from the server's shell: create them, set passwords, accept registrations.

    efficharge add-user EMAIL          a login (asks for the password)
    efficharge set-password EMAIL
    efficharge pending                 registrations waiting, with their "who are you"
    efficharge approve EMAIL           accept one: they can log in
    efficharge reject EMAIL            delete a waiting registration
    efficharge add-virtual-plug EMAIL  a virtual plug for an existing user

New accounts start without a plug: Settings welcomes them (connect Meross, or try a
virtual plug first).

Passwords are read with ``getpass`` (not echoed, never in the shell history).
"""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from efficharge.crypto import Vault
from efficharge.db.models import User
from efficharge.db.repo import WAITING_REGISTRATIONS, accept_registration, create_user
from efficharge.db.session import make_engine, make_sessionmaker
from efficharge.demo_setup import add_virtual_plug
from efficharge.web.auth import MIN_PASSWORD, hash_password


def ask_password() -> str:
    while True:
        password = getpass.getpass("New password: ")
        if len(password) < MIN_PASSWORD:
            print(f"At least {MIN_PASSWORD} characters, please.")
            continue
        if getpass.getpass("Repeat it: ") != password:
            print("The two entries differ, try again.")
            continue
        return password


async def _in_db(url: str, work: Callable[[AsyncSession], Awaitable[None]]) -> None:
    """Run ``work`` in one transaction."""
    engine = make_engine(url)
    try:
        async with make_sessionmaker(engine)() as db, db.begin():
            await work(db)
    finally:
        await engine.dispose()


async def _user(db: AsyncSession, email: str) -> User:
    user = await db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or user.is_demo:
        raise SystemExit(f"No user {email}.")
    return user


async def add_user(url: str, email: str, password: str, *, timezone: str, locale: str) -> None:
    email = email.strip().lower()
    if "@" not in email:
        raise SystemExit("Please give a valid email address.")

    async def work(db):
        if await db.scalar(select(User.id).where(User.email == email)):
            raise SystemExit(f"{email} already exists. Use `efficharge set-password`.")
        await create_user(db, email, hash_password(password), locale=locale, timezone=timezone)

    await _in_db(url, work)


async def set_password(url: str, email: str, password: str) -> None:
    async def work(db):
        (await _user(db, email)).password_hash = hash_password(password)

    await _in_db(url, work)


async def pending(url: str) -> list[User]:
    found: list[User] = []

    async def work(db):
        found.extend((await db.scalars(WAITING_REGISTRATIONS)).all())

    await _in_db(url, work)
    return found


async def approve(url: str, email: str) -> None:
    async def work(db):
        user = await _user(db, email)
        if user.approved_at is not None:
            raise SystemExit(f"{email} is accepted already.")
        accept_registration(user)

    await _in_db(url, work)


async def reject(url: str, email: str) -> None:
    async def work(db):
        user = await _user(db, email)
        if user.approved_at is not None:
            raise SystemExit(f"{email} is accepted: remove accounts in the app, not here.")
        await db.delete(user)

    await _in_db(url, work)


async def add_plug(url: str, vault: Vault, email: str) -> None:
    async def work(db):
        await add_virtual_plug(db, vault, await _user(db, email))

    await _in_db(url, work)


def run(
    command: str, url: str, vault: Vault, email: str | None, *, timezone: str, locale: str
) -> None:
    match command:
        case "add-user":
            password = ask_password()
            asyncio.run(add_user(url, email, password, timezone=timezone, locale=locale))
            print(f"Created {email}. Log in at /login.")
        case "set-password":
            asyncio.run(set_password(url, email, ask_password()))
            print(f"Password of {email} changed.")
        case "pending":
            users = asyncio.run(pending(url))
            if not users:
                print("No registrations waiting.")
            for user in users:
                print(f"{user.email}  (registered {user.created_at:%Y-%m-%d %H:%M} UTC)")
                for line in (user.about or "–").splitlines():
                    print(f"    {line}")
        case "approve":
            asyncio.run(approve(url, email))
            print(f"Accepted {email}: they can log in now.")
        case "reject":
            asyncio.run(reject(url, email))
            print(f"Deleted the registration of {email}.")
        case "add-virtual-plug":
            asyncio.run(add_plug(url, vault, email))
            print(f"Added a virtual plug to {email}.")
