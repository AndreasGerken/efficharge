"""/admin: registrations waiting for approval, and who uses the instance.

Only for the accounts in ``EFFICHARGE_ADMINS``; everyone else gets a 404 (the page
doesn't exist for them). The same actions work over SSH (``efficharge approve``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_datetime
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from efficharge.db.models import Cycle, Device, DeviceAccount, User, VisitCount
from efficharge.db.repo import WAITING_REGISTRATIONS, accept_registration
from efficharge.web import i18n, visits

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


VISIT_DAYS = 14  # the table: one row per day
TOP_PAGES_DAYS = 7


async def visit_numbers(request: Request) -> dict:
    """Visitors and page views of the last days, and the most viewed pages."""
    runtime = rt(request)
    await runtime.visits.flush(runtime.sessions)  # include the last minute
    today = runtime.clock().astimezone(runtime.visits.tz).date()
    days = [today - timedelta(days=n) for n in range(VISIT_DAYS)]
    async with runtime.readers() as db:  # a new read: it sees what the flush just wrote
        rows = (
            await db.execute(
                select(
                    VisitCount.day, VisitCount.page, VisitCount.views, VisitCount.visitors
                ).where(VisitCount.day >= days[-1].isoformat())
            )
        ).all()
        first = await db.scalar(select(func.min(VisitCount.day)))  # counting began that day
    by_day = {(r.day, r.page): r for r in rows}
    days = [d for d in days if first is not None and d.isoformat() >= first]

    def day_row(d: date) -> tuple[str, int, int, int]:
        total = by_day.get((d.isoformat(), visits.TOTAL))
        demos = by_day.get((d.isoformat(), visits.DEMO_STARTS))
        label = format_date(d, "EEE d MMM", locale=i18n.get_locale())
        return (
            label,
            total.visitors if total else 0,
            total.views if total else 0,
            demos.views if demos else 0,
        )

    since = (today - timedelta(days=TOP_PAGES_DAYS - 1)).isoformat()
    pages: dict[str, list[int]] = {}
    for r in rows:
        if r.day >= since and r.page not in (visits.TOTAL, visits.DEMO_STARTS):
            seen = pages.setdefault(r.page, [0, 0])
            seen[0] += r.views
            seen[1] += r.visitors
    top = sorted(pages.items(), key=lambda kv: -kv[1][0])[:10]
    week = [day_row(d) for d in days[:TOP_PAGES_DAYS]]
    return {
        "visit_days": [day_row(d) for d in days],
        "visit_week": (sum(r[1] for r in week), sum(r[2] for r in week), sum(r[3] for r in week)),
        "top_pages": [(page, views, people) for page, (views, people) in top],
        "top_pages_days": TOP_PAGES_DAYS,
    }


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
            **await visit_numbers(request),
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
