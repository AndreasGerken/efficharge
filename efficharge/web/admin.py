"""/admin: registrations waiting for approval, and who uses the instance.

Only for the accounts in ``EFFICHARGE_ADMINS``; everyone else gets a 404 (the page
doesn't exist for them). The same actions work over SSH (``efficharge approve``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from babel.dates import format_datetime
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from efficharge.db.models import Cycle, Device, DeviceAccount, User
from efficharge.db.repo import WAITING_REGISTRATIONS, accept_registration
from efficharge.web import i18n

# app.py imports this module at the end of create_app, when it is fully loaded. The names
# must be module globals: FastAPI resolves the (string) annotations of the routes here.
from efficharge.web.app import DB, Csrf, CurrentUser, _is_htmx, render, rt

logger = logging.getLogger(__name__)


def require_admin(request: Request, user: CurrentUser) -> User:
    if not rt(request).config.is_admin(user):
        raise HTTPException(404)
    return user


Admin = Depends(require_admin)


def _when(dt: datetime | None, tz: ZoneInfo) -> str:
    if dt is None:
        return "–"
    return format_datetime(dt, "d MMM yyyy, HH:mm", tzinfo=tz, locale=i18n.get_locale())


async def waiting_count(db) -> int:
    """For the settings page: how many registrations wait."""
    return await db.scalar(select(func.count()).select_from(WAITING_REGISTRATIONS.subquery()))


def register_admin_routes(app: FastAPI) -> None:
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request, db: DB, user: User = Admin, done: str = ""):
        tz = ZoneInfo(user.timezone)
        waiting = (await db.scalars(WAITING_REGISTRATIONS)).all()
        real_plugs = dict(
            (
                await db.execute(
                    select(Device.user_id, func.count())
                    .join(DeviceAccount, Device.account_id == DeviceAccount.id)
                    .where(DeviceAccount.kind != "simulated")
                    .group_by(Device.user_id)
                )
            ).all()
        )
        charges = dict(
            (
                await db.execute(
                    select(Device.user_id, func.count())
                    .join(Cycle, Cycle.device_id == Device.id)
                    .where(Cycle.end_at.is_not(None), Cycle.deleted_at.is_(None))
                    .group_by(Device.user_id)
                )
            ).all()
        )
        users = (
            await db.scalars(
                select(User)
                .where(User.approved_at.is_not(None), User.is_demo.is_(False))
                .order_by(User.approved_at.desc())
            )
        ).all()
        demo = rt(request).demo
        return render(
            request,
            "admin.html",
            done=done,
            waiting=[(u, _when(u.created_at, tz)) for u in waiting],
            users=[
                (u, _when(u.approved_at, tz), real_plugs.get(u.id, 0), charges.get(u.id, 0))
                for u in users
            ],
            demos=len(demo.demos) if demo is not None and rt(request).config.demo else None,
        )

    async def waiting_user(db, user_id: int) -> User:
        u = await db.get(User, user_id)
        if u is None or u.is_demo or u.approved_at is not None:
            raise HTTPException(404)  # gone, or accepted already (a second click)
        return u

    def show_done(request: Request, done: str) -> Response:
        """Back to /admin with a notice (htmx: the reject button, after its confirm)."""
        target = f"/admin?done={done}"
        if _is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.post("/admin/users/{user_id}/approve", dependencies=[Csrf])
    async def approve(request: Request, db: DB, user_id: int, admin: User = Admin):
        u = await waiting_user(db, user_id)
        accept_registration(u)
        await db.commit()
        logger.info("registration accepted by an admin: user %s", user_id)
        return show_done(request, "approved")

    @app.post("/admin/users/{user_id}/reject", dependencies=[Csrf])
    async def reject(request: Request, db: DB, user_id: int, admin: User = Admin):
        u = await waiting_user(db, user_id)
        await db.delete(u)
        await db.commit()
        logger.info("registration rejected by an admin: user %s", user_id)
        return show_done(request, "rejected")
