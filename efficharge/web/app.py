"""FastAPI app: server-rendered pages (Jinja + HTMX), chart JSON, and the workers.

One process runs everything: the web layer never talks to a
plug. It sends commands to the supervisor and reads state from the DB.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, available_timezones

from babel.dates import format_date
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from efficharge.config import Config
from efficharge.control import maintenance
from efficharge.control.machine import (
    ChargeNow,
    ManualPlugIn,
    Phase,
    PlugOn,
    ReadyBy,
    Replan,
    StopCharging,
)
from efficharge.control.planning import expected_kwh, past_durations, planner_inputs
from efficharge.control.runner import PollNow
from efficharge.control.serialize import state_from_dict
from efficharge.control.supervisor import Supervisor
from efficharge.crypto import Vault
from efficharge.db.models import (
    Cycle,
    DeadlineRuleRow,
    Device,
    DeviceAccount,
    DeviceSettings,
    DeviceStateRow,
    User,
)
from efficharge.db.repo import create_user
from efficharge.db.session import make_engine, make_sessionmaker, readers_for
from efficharge.demo_setup import FAST, DemoFull, DemoInstance, DemoManager
from efficharge.energy.client import ATTRIBUTION, EnergyChartsClient
from efficharge.energy.fetcher import EnergyFetcher
from efficharge.planner.duration import LAST_N
from efficharge.planner.window import PRESETS, Priority
from efficharge.web import auth, example, i18n
from efficharge.web.data import (
    chart_payload,
    cumulative,
    deadline_info,
    device_rules,
    fallback_text,
    finished_cycles,
    live_numbers,
    preview_plan,
    recent_cycles,
    remaining_time,
    rule_overlaps,
    rule_text,
    totals,
)
from efficharge.web.explain import STEPS, cycle_chips, explain, priority_decides, step_labels

logger = logging.getLogger(__name__)
HERE = Path(__file__).parent
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Runtime:
    """What routes need besides the DB; lives on ``app.state.rt``."""

    config: Config
    sessions: async_sessionmaker
    vault: Vault
    clock: Clock
    supervisor: Supervisor | None = None
    fetcher: EnergyFetcher | None = None
    demo: DemoManager | None = None

    @property
    def readers(self) -> async_sessionmaker:
        return readers_for(self.sessions)

    def clock_for(self, device_id: int) -> Clock:
        """Demo devices have their own (fast-forwardable) clock; everything else is real."""
        return self.supervisor.clock_for(device_id) if self.supervisor else self.clock


# ── templates ──

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(
    lambda s: i18n._t().gettext(s),
    lambda s, p, n: i18n._t().ngettext(s, p, n),
    newstyle=True,
)
templates.env.globals.update(
    fmt_eur=i18n.fmt_eur,
    fmt_ct=i18n.fmt_ct,
    fmt_num=i18n.fmt_num,
    fmt_duration=i18n.fmt_duration,
    fmt_day_time=i18n.fmt_day_time,
    get_locale=i18n.get_locale,
    weekday_names=i18n.weekday_names,
    weekdays_text=i18n.weekdays_text,
    # versioned URLs: a changed CSS/JS file gets a new URL, so no browser serves an old one
    static=lambda path: f"/static/{path}?v={STATIC_VERSION}",
    attribution=ATTRIBUTION,
    within_hours=lambda: WITHIN_HOURS,
    rule_text=rule_text,
    fallback_text=fallback_text,
)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        name,
        {
            "csrf_token": auth.csrf_token(request),
            "user": getattr(request.state, "user", None),
            "is_demo": request.session.get("demo", False),
            "registration_open": rt(request).config.registration_open,
            "demo_enabled": rt(request).config.demo,
            "about_page": rt(request).config.about_page,
            "has_imprint": rt(request).config.has_imprint,
            "coffee_url": rt(request).config.coffee_url,
            "support_email": rt(request).config.contact_email,
            "source_url": rt(request).config.source_url,
            **ctx,
        },
        status_code=status_code,
    )


# ── dependencies ──


def rt(request: Request) -> Runtime:
    return request.app.state.rt


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """GET pages only read: a read session (no lock). Everything else may write."""
    runtime = rt(request)
    make = runtime.readers if request.method in ("GET", "HEAD") else runtime.sessions
    async with make() as session:
        yield session


DB = Annotated[AsyncSession, Depends(get_db)]


async def locale_and_user(request: Request) -> None:
    """Runs for every request: pick the language, load the user."""
    lang = request.query_params.get("lang")
    if lang in i18n.SUPPORTED:
        request.session["locale"] = lang
    request.state.user = None
    uid = request.session.get("uid")
    if uid is not None:
        async with rt(request).readers() as s:
            request.state.user = await s.get(User, uid)
        if request.state.user is None or not auth.session_valid(request, request.state.user):
            # user gone, or the password changed since the login. A public demo's user is
            # deleted after 2 h idle and at every restart: that visitor gets a new demo,
            # not a login page they have no account for (see _login_required)
            was_demo = bool(request.session.get("demo"))
            _end_session(request)
            if was_demo:  # kept until a new demo or a login replaces the session
                request.session["demo_ended"] = True
            request.state.user = None
        elif lang in i18n.SUPPORTED and request.state.user.locale != lang:
            # DE | EN in the top bar: the same as the language in the settings (stored)
            async with rt(request).sessions() as s, s.begin():
                (await s.get(User, uid)).locale = lang
            request.state.user.locale = lang
    i18n.set_locale(
        request.session.get("locale") or i18n.negotiate(request.headers.get("accept-language"))
    )


class LoginRequired(Exception):
    pass


def current_user(request: Request) -> User:
    user = request.state.user
    if user is None:
        raise LoginRequired
    return user


CurrentUser = Annotated[User, Depends(current_user)]
Csrf = Depends(auth.check_csrf)


async def owned_device(device_id: int, user: CurrentUser, db: DB) -> Device:
    device = await db.get(Device, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(404)
    # Release the transaction (BEGIN IMMEDIATE holds the write lock, see db/session.py):
    # handlers may now talk to runners or open other sessions without deadlocking.
    await db.commit()
    return device


OwnedDevice = Annotated[Device, Depends(owned_device)]


# ── page helpers ──


async def virtual_plug(request: Request, device: Device) -> DemoInstance | None:
    """The simulation of a virtual plug (started if needed); None for a real plug. The
    caller made sure the device belongs to the user (``OwnedDevice``)."""
    manager = rt(request).demo
    return await manager.open(device.id) if manager is not None else None


async def demo_view(request: Request, demo: DemoInstance | None) -> dict | None:
    """A virtual plug's simulation, for its extra buttons (battery, speed)."""
    if demo is None:
        return None
    dev = rt(request).demo.sim_device(demo)
    moved = abs((demo.clock() - demo.clock.real_now()).total_seconds()) > 60
    return {
        "fast": demo.clock.speed > 1,
        "moved": moved or demo.clock.speed > 1,  # "back to now" makes sense
        "battery": dev is not None and dev.battery is not None,
    }


#: the "who are you" text of a registration
ABOUT_MAX = 1000
#: registrations waiting for approval at most (against bots filling the list)
MAX_PENDING = 50

#: no reading from the plug for this long (polled every 30 s): it is offline or gone
OFFLINE_AFTER = timedelta(minutes=3)
#: "refresh" reads the plug through the vendor cloud, which bans accounts that ask too often
REFRESH_EVERY_S = 10
#: a one-off "ready by" at most this far ahead (the dashboard offers three days)
READY_BY_MAX = timedelta(days=14)
#: deadline rules per plug
MAX_RULES = 20


async def user_plugs(db: AsyncSession, user_id: int) -> list[tuple[Device, bool]]:
    """The user's plugs with "is virtual", real ones first (the dashboard's tabs)."""
    rows = (
        await db.execute(
            select(Device, DeviceAccount.kind == "simulated")
            .join(DeviceAccount, DeviceAccount.id == Device.account_id)
            .where(Device.user_id == user_id)
            .order_by(DeviceAccount.kind == "simulated", Device.id)
        )
    ).all()
    return [(device, bool(virtual)) for device, virtual in rows]


async def waiting_kwh(db: AsyncSession, cycle: Cycle) -> float | None:
    """The energy a charge that hasn't started yet will probably take (for its estimates)."""
    if cycle.end_at is not None or cycle.actual_start is not None or not cycle.plan:
        return None
    return await expected_kwh(db, cycle.device_id, timedelta(seconds=cycle.plan["duration_s"]))


async def device_view(request: Request, db: AsyncSession, device: Device, user: User) -> dict:
    virtual = await virtual_plug(request, device)  # first: it may start over, on its clock
    now = rt(request).clock_for(device.id)()
    tz = ZoneInfo(user.timezone)
    row = await db.get(DeviceStateRow, device.id)
    state = state_from_dict(row.data if row else None)
    cycle = await db.get(Cycle, row.current_cycle_id) if row and row.current_cycle_id else None
    last = await db.scalar(
        select(Cycle)
        .where(Cycle.device_id == device.id, Cycle.end_at.is_not(None), Cycle.deleted_at.is_(None))
        .order_by(Cycle.end_at.desc())
        .limit(1)
    )
    account = await db.get(DeviceAccount, device.account_id) if device.account_id else None
    settings = await db.get(DeviceSettings, device.id)
    remaining = await remaining_time(db, device.id, cycle, state, now) if cycle else None
    ready = state.phase in (Phase.IDLE, Phase.DONE)
    preview = (
        await preview_plan(db, device.id, rt(request).config.bidding_zone, now) if ready else None
    )
    rule = await deadline_info(db, device.id, state, tz, now)  # shown in every state
    active = cycle is not None and state.phase in (
        Phase.PLANNED,
        Phase.CHARGING,
        Phase.PAUSED_BY_USER,
    )
    zone = rt(request).config.bidding_zone
    live = await live_numbers(db, cycle, zone, now) if active else None
    estimate_kwh = await waiting_kwh(db, cycle) if active else None
    preview_kwh = await expected_kwh(db, device.id, preview.duration) if preview else None
    await db.commit()  # demo_view opens its own session: don't hold the lock meanwhile
    status = explain(
        state,
        cycle,
        last,
        tz,
        now,
        metered=settings.metered if settings else True,
        power_w=row.last_power_w if row else None,
        remaining=remaining,
        preview=preview,
        preview_kwh=preview_kwh,
    )
    if state.phase is Phase.PLANNED or ready:
        shown_plan = state.plan if state.phase is Phase.PLANNED else preview
        status.show_priority = priority_decides(shown_plan)
        # the green forecast doesn't reach far enough yet: the price alone decides for now
        status.price_only = bool(shown_plan and shown_plan.green_ignored)
    if active:
        # the same labels as this charge has in the list of charges
        status.chips = cycle_chips(cycle, tz, now, live, state=False, expected_kwh=estimate_kwh)
    plug_count = await db.scalar(
        select(func.count()).select_from(Device).where(Device.user_id == user.id)
    )
    return {
        "device": device,
        "row": row,
        "status": status,
        "priority": _priority_name(user.price_weight),
        "priorities": [
            ("eco", "🌱", i18n._("Eco first")),
            ("balanced", "⚖️", i18n._("Balanced")),
            ("money", "💶", i18n._("Cost first")),
        ],
        "plug_count": plug_count,
        "hints": {
            "eco": i18n._("Most green power. Price breaks ties."),
            "balanced": i18n._("Green power and price count equally."),
            "money": i18n._("Lowest price. Pays off with a dynamic tariff."),
        },
        "rule": rule,
        "recent_chips": cycle_chips(status.recent, tz, now) if status.recent else [],
        "steps": list(zip(STEPS, step_labels(), strict=True)),
        "is_sim": account is not None and account.kind == "simulated",
        "needs_reauth": account is not None and account.status == "needs_reauth",
        # in real time: a fast-forwarding virtual plug is read every 0.1 s, which is minutes
        # on its clock
        "last_seen_ago": (
            (now - row.last_seen) / (virtual.clock.speed if virtual else 1.0)
            if row and row.last_seen
            else None
        ),
        # offline, or not found in the account: to the user both mean the same. A virtual
        # plug runs in this process: it can't be offline
        "offline_since": (
            row.last_seen
            if virtual is None and row and row.last_seen and now - row.last_seen > OFFLINE_AFTER
            else None
        ),
        "now": now,
        "tz": tz,
        "demo": await demo_view(request, virtual),
        "ready_days": _ready_days(now, tz),
        "ready_default": _ready_default(state, now, tz),
    }


# ── app factory ──


def create_app(
    config: Config,
    *,
    sessions: async_sessionmaker | None = None,
    clock: Clock = utc_now,
    start_workers: bool = True,
    supervisor: Supervisor | None = None,
    fetch_clock: Clock = utc_now,
    energy_client: EnergyChartsClient | None = None,
) -> FastAPI:
    vault = Vault.from_config(config.data_keys)
    engine = None
    if sessions is None:
        engine = make_engine(config.database_url)
        sessions = make_sessionmaker(engine)
    runtime = Runtime(config, sessions, vault, clock, supervisor=supervisor)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        i18n.load_catalogs()
        tasks: list[asyncio.Task] = []
        if start_workers:
            runtime.supervisor = Supervisor(
                sessions,
                vault,
                zone=config.bidding_zone,
                clock=clock,
                poll_interval=config.poll_interval,
            )
            client = energy_client or EnergyChartsClient(
                bidding_zone=config.bidding_zone, country=config.country
            )
            runtime.fetcher = EnergyFetcher(
                client,
                sessions,
                config.bidding_zone,
                fetch_clock,
                on_new_prices=runtime.supervisor.broadcast_replan,
                interval=config.fetch_interval,
            )
            tasks.append(asyncio.create_task(runtime.fetcher.run(), name="fetcher"))
            tasks.append(
                asyncio.create_task(
                    maintenance.run(runtime.supervisor.store, clock), name="maintenance"
                )
            )
            # virtual plugs (every account has one; ``config.demo``: also the public demo)
            runtime.demo = DemoManager(
                sessions, vault, runtime.supervisor, zone=config.bidding_zone
            )
            await runtime.demo.delete_leftovers()  # public demos of a previous run
            tasks.append(asyncio.create_task(runtime.demo.run(), name="demo"))
            await runtime.supervisor.start()
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            # wait until they are really gone (no DB call may outlive the event loop)
            await asyncio.gather(*tasks, return_exceptions=True)
            if runtime.demo is not None:
                await runtime.demo.close()
            if runtime.supervisor is not None:
                await runtime.supervisor.stop()
            if runtime.fetcher is not None:
                await runtime.fetcher.client.aclose()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="efficharge",
        lifespan=lifespan,
        dependencies=[Depends(locale_and_user)],
        # no public API yet: don't publish the route list (enable it with the public API)
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.rt = runtime
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie="ec_session",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=config.cookie_secure,
    )
    app.add_middleware(
        SecurityHeaders, hsts=config.cookie_secure, noindex=not config.allow_indexing
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.session.get("demo_ended") and rt(request).config.demo:
            target = "/demo"  # the demo ended (restart, or idle): start a new one
        else:
            target = "/login"
        if _is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": target})
        if target == "/login":
            target = f"/login?next={request.url.path}"
        return RedirectResponse(target, status_code=303)

    _routes(app)
    from efficharge.web.accounts import register_account_routes
    from efficharge.web.admin import register_admin_routes

    app.state.pick_cache = {}
    app.state.refreshed = {}  # device id → time.monotonic() of its last "refresh"
    register_account_routes(app)
    register_admin_routes(app)
    return app


class SecurityHeaders:
    """Strict CSP: only our own scripts and styles, no inline code."""

    HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            b"connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        ),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"x-frame-options", b"DENY"),
        # no camera, microphone, location, … for this site or anything it could load
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
        # other windows (a link that opened us, or one we open) can't reach into this one
        (b"cross-origin-opener-policy", b"same-origin"),
    )

    HSTS = (b"strict-transport-security", b"max-age=31536000")
    #: until launch (EFFICHARGE_ALLOW_INDEXING): keep the instance out of search engines
    NOINDEX = (b"x-robots-tag", b"noindex, nofollow")

    def __init__(self, app, hsts: bool = False, noindex: bool = True) -> None:
        self.app = app
        self.headers = (
            *self.HEADERS,
            *((self.HSTS,) if hsts else ()),
            *((self.NOINDEX,) if noindex else ()),
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], *self.headers]
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── routes ──


def _routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request):
        if request.state.user is not None and not request.session.get("demo"):
            return RedirectResponse("/devices", status_code=303)
        config = rt(request).config
        if not config.about_page or (not config.demo and not config.registration_open):
            # private instance, or no start page (yet): nothing to see without a login
            return RedirectResponse("/login", status_code=303)
        return _landing(request)

    @app.get("/about", response_class=HTMLResponse)
    async def about(request: Request):
        """The start page for everyone, also when logged in (``/`` leads to the dashboard
        then): linked from the top bar and the footer. 404 with ``EFFICHARGE_ABOUT_PAGE=0``."""
        if not rt(request).config.about_page:
            raise HTTPException(404)
        return _landing(request)

    def _landing(request: Request) -> HTMLResponse:
        c, zone = example.cycle(), example.tz()

        def hm(t: datetime) -> str:
            return i18n.fmt_time(t, zone)

        return render(
            request,
            "landing.html",
            example_chips=cycle_chips(c, zone, example.now()),
            example_times={
                "plugged": hm(c.plugged_at),
                "ready": hm(c.deadline),
                "start": hm(c.actual_start),
                "end": hm(c.end_at),
                "date": format_date(
                    c.plugged_at.astimezone(zone), "long", locale=i18n.get_locale()
                ),
            },
        )

    @app.get("/example-chart.json")
    async def example_chart():
        """The start page's example charge, in the shape of a plug's chart.json."""
        return JSONResponse(example.payload())

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots(request: Request):
        if not rt(request).config.allow_indexing:
            return "User-agent: *\nDisallow: /\n"
        # only the public pages are worth indexing; the rest needs a login anyway
        return (
            "User-agent: *\nAllow: /$\nAllow: /about\nAllow: /privacy\nAllow: /impressum\n"
            "Disallow: /\n"
        )

    @app.get("/demo")
    async def demo(request: Request, next: str | None = None):
        """Your own demo plug: reused while it exists, otherwise a fresh one."""
        manager = rt(request).demo
        if manager is None or not rt(request).config.demo:
            raise HTTPException(404)
        demo = manager.get(request.session.get("demo_device"))
        user = request.state.user
        if demo is not None and (user is None or user.id != demo.user_id):
            demo = None  # an old cookie: ids are reused, that plug is someone else's now
        if demo is None:
            ip = _client_ip(request)
            if not auth.demo_limiter.hit(ip):  # a bot following the link in a loop
                return render(request, "demo_full.html", 429, too_many=True)
            try:
                demo = await manager.create(request.session.get("locale") or i18n.get_locale())
            except DemoFull:
                return render(request, "demo_full.html", 503)
            locale = request.session.get("locale") or i18n.get_locale()
            async with rt(request).readers() as s:
                auth.login_session(request, await s.get(User, demo.user_id), locale)
            request.session["demo"] = True
            request.session["demo_device"] = demo.device_id
        return RedirectResponse(_safe_next(next, f"/devices/{demo.device_id}"), status_code=303)

    @app.post("/devices/{device_id}/demo", response_class=HTMLResponse, dependencies=[Csrf])
    async def demo_control(
        request: Request,
        user: CurrentUser,
        device: OwnedDevice,
        action: Annotated[str, Form()],
    ):
        demo = await virtual_plug(request, device)
        if demo is None:
            raise HTTPException(404)
        manager = rt(request).demo
        match action:
            case "plug_in":
                await manager.plug_in(demo)
            case "unplug":
                await manager.unplug(demo)
            case "button":
                await manager.press_button(demo)
            case "fast":
                await manager.set_speed(demo, FAST)
            case "normal":
                await manager.set_speed(demo, 1.0)
            case "reset":
                await manager.reset(demo)
            case _:
                raise HTTPException(400, "unknown action")
        return await _status_after(request, device, user)

    # auth

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/devices", deleted: bool = False):
        return render(request, "login.html", next=next, error=None, deleted=deleted)

    @app.post("/login", dependencies=[Csrf])
    async def login(
        request: Request,
        db: DB,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/devices",
    ):
        email = email.strip().lower()[:320]
        ip = _client_ip(request)

        def refuse(status: int, error: str) -> HTMLResponse:
            return render(request, "login.html", status, next=next, error=error)

        if not (auth.login_limiter.hit(f"{ip}|{email}") and auth.ip_limiter.hit(ip)):
            logger.warning("login refused: too many attempts from one address")
            return refuse(429, i18n._("Too many attempts. Please wait 15 minutes."))
        # Counted for unknown emails too, so the answer doesn't tell which accounts exist.
        if auth.account_limiter.full(email):
            logger.warning("login refused: too many wrong passwords for one account this hour")
            return refuse(429, i18n._("Too many wrong passwords. Please try again in an hour."))
        user = await db.scalar(select(User).where(User.email == email))
        try:
            ok = auth.check_password(user.password_hash if user else None, password)
        except auth.Busy:
            logger.warning("login refused: too many password checks overall")
            return refuse(429, i18n._("Too many logins right now. Please try again in a minute."))
        if not ok or user is None or user.is_demo:
            auth.account_limiter.hit(email)
            return refuse(400, i18n._("Email or password is wrong."))
        if user.approved_at is None:
            return render(
                request,
                "login.html",
                403,
                next=next,
                error=i18n._(
                    "Your registration hasn't been accepted yet. You can log in as soon as it is."
                ),
            )
        if auth.needs_rehash(user.password_hash):
            user.password_hash = auth.hash_password(password)
            await db.commit()
        auth.login_session(request, user, user.locale)
        target = _safe_next(next, "/devices")
        return RedirectResponse(target, status_code=303)

    @app.get("/register", response_class=HTMLResponse)
    async def register_form(request: Request):
        if not rt(request).config.registration_open:
            raise HTTPException(404)
        return render(request, "register.html", error=None)

    @app.post("/register", dependencies=[Csrf])
    async def register(
        request: Request,
        db: DB,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        about: Annotated[str, Form()] = "",
        website: Annotated[str, Form()] = "",  # the honeypot: hidden, only bots fill it in
    ):
        """A registration waits until the admin accepts it (``efficharge approve``)."""
        if not rt(request).config.registration_open:
            raise HTTPException(403)
        if website:  # a bot: looks like success to it, nothing is stored
            logger.info("registration dropped: honeypot field filled in")
            return render(request, "register.html", done=True)
        email, about = email.strip().lower(), about.strip()
        ip = _client_ip(request)
        pending = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.approved_at.is_(None), User.is_demo.is_(False))
        )
        error = None
        if pending >= MAX_PENDING:
            error = i18n._("Registration is full right now. Please try again in a few days.")
        elif "@" not in email or len(email) > 320:
            error = i18n._("Please enter a valid email address.")
        elif len(password) < auth.MIN_PASSWORD:
            error = i18n._("The password needs at least %(n)d characters.", n=auth.MIN_PASSWORD)
        elif len(password) > auth.MAX_PASSWORD:
            error = i18n._("The password can have at most %(n)d characters.", n=auth.MAX_PASSWORD)
        elif not about:
            error = i18n._("Please write a few words about who you are.")
        elif len(about) > ABOUT_MAX:
            error = i18n._("Please keep it under %(n)d characters.", n=ABOUT_MAX)
        elif not auth.register_limiter.hit(ip):  # last: typos don't count against a person
            error = i18n._("Too many registrations from your address. Please try again later.")
        if error:
            return render(request, "register.html", 400, error=error, about=about)
        if await db.scalar(select(User.id).where(User.email == email)):
            # The same answer as for a new registration: the form must not tell anyone
            # which emails have an account. The owner knows their account anyway.
            logger.info("registration for an email that has an account: nothing changed")
            return render(request, "register.html", done=True)
        try:
            password_hash = auth.new_password_hash(password)
        except auth.Busy:
            error = i18n._("Too many registrations right now. Please try again in a minute.")
            return render(request, "register.html", 429, error=error, about=about)
        user = await create_user(
            db,
            email,
            password_hash,
            locale=i18n.get_locale(),
            timezone=rt(request).config.default_timezone,
            about=about,
            approved=False,
        )
        await db.commit()
        logger.info("new registration %d waits for approval", user.id)
        return render(request, "register.html", done=True)

    @app.post("/logout", dependencies=[Csrf])
    async def logout(request: Request):
        _end_session(request)
        return RedirectResponse("/", status_code=303)

    # devices

    @app.get("/devices", response_class=HTMLResponse)
    async def devices(request: Request, db: DB, user: CurrentUser):
        """The dashboard: the plug looked at last (each plug is a tab), else the first."""
        ids = [d.id for d, _virtual in await user_plugs(db, user.id)]
        if not ids:  # a new account: Settings welcomes it (connect a plug, or try one)
            return RedirectResponse("/settings", status_code=303)
        last = request.session.get("last_device")
        return RedirectResponse(f"/devices/{last if last in ids else ids[0]}", status_code=303)

    @app.get("/devices/{device_id}", response_class=HTMLResponse)
    async def dashboard(request: Request, db: DB, user: CurrentUser, device: OwnedDevice):
        request.session["last_device"] = device.id
        return render(
            request,
            "dashboard.html",
            plugs=await user_plugs(db, user.id),
            **await device_view(request, db, device, user),
        )

    @app.get("/devices/{device_id}/status", response_class=HTMLResponse)
    async def status(request: Request, db: DB, user: CurrentUser, device: OwnedDevice):
        return render(request, "_status.html", **await device_view(request, db, device, user))

    @app.get("/devices/{device_id}/chart.json")
    async def chart(request: Request, db: DB, user: CurrentUser, device: OwnedDevice):
        row = await db.get(DeviceStateRow, device.id)
        state = state_from_dict(row.data if row else None)
        cycle = await db.get(Cycle, row.current_cycle_id) if row and row.current_cycle_id else None
        clock = rt(request).clock_for(device.id)
        payload = await chart_payload(
            db,
            device,
            state,
            cycle,
            rt(request).config.bidding_zone,
            ZoneInfo(user.timezone),
            clock(),
        )
        # a fast-forwarding demo: the browser animates the "now" between two refreshes
        speed = getattr(clock, "speed", 1.0)
        limit = getattr(clock, "limit", None)
        payload["clock"] = {
            "speed": speed,
            "until": int(limit.timestamp() * 1000) if speed > 1 and limit else None,
        }
        return JSONResponse(payload, headers={"cache-control": "no-store"})

    @app.post("/devices/{device_id}/command", response_class=HTMLResponse, dependencies=[Csrf])
    async def command(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        action: Annotated[str, Form()],
        ready_by: Annotated[str | None, Form()] = None,
        ready_day: Annotated[str | None, Form()] = None,
        ready_time: Annotated[str | None, Form()] = None,
    ):
        if ready_day and ready_time:  # the dashboard's day + time selects
            ready_by = f"{ready_day}T{ready_time}"
        sup = rt(request).supervisor
        if sup is None or device.id not in sup.runners:
            raise HTTPException(503, "device is not running")
        tz = ZoneInfo(user.timezone)
        match action:
            case "charge_now":
                sup.send(device.id, ChargeNow())
            case "stop":
                sup.send(device.id, StopCharging())
            case "plug_on":
                sup.send(device.id, PlugOn())
            case "plugged_in":
                sup.send(device.id, ManualPlugIn())
            case "refresh":
                refreshed = request.app.state.refreshed
                if time.monotonic() - refreshed.get(device.id, -REFRESH_EVERY_S) >= REFRESH_EVERY_S:
                    refreshed[device.id] = time.monotonic()
                    await _refresh(request, device.id)
                return await _status_after(request, device, user)
            case "ready_by" if ready_by:
                try:
                    when = datetime.fromisoformat(ready_by).replace(tzinfo=tz)
                except ValueError:
                    raise HTTPException(400, "ready_by must be a date and time") from None
                now = rt(request).clock_for(device.id)()
                if when <= now:
                    raise HTTPException(400, "time is in the past")
                if when > now + READY_BY_MAX:
                    raise HTTPException(400, "at most 14 days ahead")
                sup.send(device.id, ReadyBy(when.astimezone(UTC)))
            case _:
                raise HTTPException(400, "unknown action")
        return await _status_after(request, device, user)

    # settings

    # settings: the user's (priority, plugs, account) and each plug's (charger, rules)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request,
        db: DB,
        user: CurrentUser,
        saved: str = "",
        delete_error: str = "",
        removed: str = "",
    ):
        from efficharge.web.accounts import plug_accounts  # (accounts imports this module)
        from efficharge.web.admin import waiting_count

        return render(
            request,
            "settings.html",
            waiting=await waiting_count(db) if rt(request).config.is_admin(user) else None,
            saved=saved,
            removed=removed,
            delete_error={
                "password": i18n._("The password is wrong. Nothing was deleted."),
                "limit": i18n._("Too many attempts. Please wait 15 minutes."),
            }.get(delete_error),
            timezones=sorted(z for z in available_timezones() if "/" in z),
            **await plug_accounts(request, db, user),
        )

    @app.post("/settings/delete", dependencies=[Csrf])
    async def account_delete(
        request: Request, db: DB, user: CurrentUser, password: Annotated[str, Form()]
    ):
        """GDPR Art. 17: the user deletes their own account and everything of it."""
        _no_demo_edits(request)
        u = await db.get(User, user.id)
        ip = _client_ip(request)
        # the same limit as the login: a stolen session can't guess the password here
        if not (auth.login_limiter.hit(f"{ip}|{u.email}") and auth.ip_limiter.hit(ip)):
            return RedirectResponse("/settings?delete_error=limit#delete", status_code=303)
        try:
            ok = auth.check_password(u.password_hash, password)
        except auth.Busy:
            return RedirectResponse("/settings?delete_error=limit#delete", status_code=303)
        if not ok:
            return RedirectResponse("/settings?delete_error=password#delete", status_code=303)
        account_ids = (
            await db.scalars(select(DeviceAccount.id).where(DeviceAccount.user_id == u.id))
        ).all()
        await db.commit()  # stopping runners needs the DB: release the lock first
        runtime = rt(request)
        for account_id in account_ids:  # stop the plugs first, so nothing writes for them
            if runtime.demo is not None:
                await runtime.demo.forget_account(account_id)  # a virtual plug
            if runtime.supervisor is not None:
                await runtime.supervisor.remove_account(account_id)
            request.app.state.pick_cache.pop(account_id, None)
        u = await db.get(User, user.id)
        if u is not None:
            await db.delete(u)  # accounts, tokens, devices, charges, samples: ON DELETE CASCADE
            await db.commit()
        logger.info("user %d deleted their account", user.id)
        _end_session(request)
        return RedirectResponse("/login?deleted=1", status_code=303)

    @app.post("/devices/{device_id}/priority", dependencies=[Csrf])
    async def device_priority(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        priority: Annotated[str, Form()],
    ):
        """The switch on a plug's page: one setting for all the user's plugs (like in the
        settings). Saves, re-plans every plug, and returns this plug's status card."""
        if priority not in Priority.__members__.values():
            raise HTTPException(400, "unknown priority")
        u = await db.get(User, user.id)
        u.price_weight = user.price_weight = PRESETS[Priority(priority)].price
        device_ids = (await db.scalars(select(Device.id).where(Device.user_id == u.id))).all()
        await db.commit()
        for device_id in device_ids:
            await _apply_settings(request, device_id)
        if _is_htmx(request):
            return await _status_after(request, device, user)
        return RedirectResponse(f"/devices/{device.id}", status_code=303)

    @app.post("/settings/account", dependencies=[Csrf])
    async def account_save(
        request: Request,
        db: DB,
        user: CurrentUser,
        locale: Annotated[str, Form()],
        timezone: Annotated[str, Form()],
    ):
        _no_demo_edits(request)
        u = await db.get(User, user.id)
        if locale in i18n.SUPPORTED:
            u.locale = locale
            request.session["locale"] = locale
        if timezone in available_timezones():
            u.timezone = timezone
        await db.commit()
        if _is_htmx(request):  # language or time zone: everything shown changes, reload
            return Response(status_code=204, headers={"HX-Refresh": "true"})
        return RedirectResponse("/settings?saved=account#account", status_code=303)

    @app.get("/account")
    async def account_page():
        return RedirectResponse("/settings#account", status_code=303)

    @app.get("/devices/{device_id}/settings", response_class=HTMLResponse)
    async def device_settings_page(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        saved: bool = False,
        welcome: bool = False,
        edit: str | None = None,  # a rule id or "fallback": that rule's form opens
    ):
        settings = await db.get(DeviceSettings, device.id)
        rules = await device_rules(db, device.id)
        return render(
            request,
            "device_settings.html",
            device=device,
            removable=await _removable(request, db, device),
            s=settings,
            rules=rules,
            overlaps=_overlap_texts(rules),
            saved=saved,
            welcome=welcome,
            **await _planned(db, device.id),
            edit_rule=edit,
        )

    @app.post("/devices/{device_id}/remove", dependencies=[Csrf])
    async def device_remove(request: Request, db: DB, user: CurrentUser, device: OwnedDevice):
        """Remove one plug (its charges, rules and readings too). The plug is switched on
        first, and its account stays connected for the other plugs."""
        if not await _removable(request, db, device):
            raise HTTPException(403, "This plug can't be removed here.")
        await db.commit()  # stopping the runner needs the DB: release the lock first
        switched_on = False
        if rt(request).supervisor is not None:
            switched_on = await rt(request).supervisor.release_device(device.id)
        current = await db.get(Device, device.id)
        if current is not None:
            await db.delete(current)  # cycles, samples, state: ON DELETE CASCADE
            await db.commit()
        logger.info("user %d removed plug %d", user.id, device.id)
        # only claim "switched on" when it worked: otherwise the user has to do it
        removed = "on" if switched_on else "off"
        return RedirectResponse(f"/settings?removed={removed}#plugs", status_code=303)

    @app.post("/devices/{device_id}/settings", dependencies=[Csrf])
    async def device_settings_save(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        default_hours: Annotated[float, Form()] = 3.0,
        battery_wh: Annotated[str, Form()] = "",
        battery_v: Annotated[str, Form()] = "",
        battery_ah: Annotated[str, Form()] = "",
    ):
        settings = await db.get(DeviceSettings, device.id)
        settings.default_duration_min = int(min(24, max(0.25, default_hours)) * 60)
        if not settings.metered:  # only plugs without metering need the battery size
            settings.battery_kwh = battery_capacity_kwh(battery_wh, battery_v, battery_ah)
        await db.commit()
        await _apply_settings(request, device.id)
        if _is_htmx(request):  # saved on change: the updated "currently planned" line
            return render(request, "_planned.html", saved=True, **await _planned(db, device.id))
        return RedirectResponse(f"/devices/{device.id}/settings?saved=1", status_code=303)

    async def rules_response(request: Request, db: AsyncSession, device: Device):
        """HTMX: only the rule list is replaced (inputs elsewhere keep their values)."""
        await _apply_settings(request, device.id)
        if not _is_htmx(request):
            return RedirectResponse(f"/devices/{device.id}/settings?saved=1#rules", 303)
        rules = await device_rules(db, device.id)
        return render(
            request,
            "_rules.html",
            device=device,
            s=await db.get(DeviceSettings, device.id),
            rules=rules,
            overlaps=_overlap_texts(rules),
            rules_saved=True,
        )

    def rule_values(target, ready_by, within_h, plugged_from, plugged_until, wd) -> dict:
        if not all(HHMM.match(v) for v in (ready_by, plugged_from, plugged_until)):
            raise HTTPException(400, "times must be HH:MM")
        mask = sum(1 << d for d in set(wd or []) if 0 <= d < 7) or 0b1111111
        values = {"weekdays": mask, "plugged_from": plugged_from, "plugged_until": plugged_until}
        if target == "within":
            return {**values, "ready_by": None, "within_min": _within_minutes(within_h)}
        return {**values, "ready_by": ready_by, "within_min": None}

    @app.post("/devices/{device_id}/rules", dependencies=[Csrf])
    async def rule_add(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        ready_by: Annotated[str, Form()] = "17:00",
        target: Annotated[str, Form()] = "ready_by",
        within_h: Annotated[str, Form()] = "6",
        plugged_from: Annotated[str, Form()] = "00:00",
        plugged_until: Annotated[str, Form()] = "00:00",
        wd: Annotated[list[int] | None, Form()] = None,
    ):
        values = rule_values(target, ready_by, within_h, plugged_from, plugged_until, wd)
        count = len(await device_rules(db, device.id))
        if count >= MAX_RULES:
            raise HTTPException(400, f"at most {MAX_RULES} rules per plug")
        db.add(DeadlineRuleRow(device_id=device.id, position=count, **values))
        await db.commit()
        return await rules_response(request, db, device)

    @app.post("/devices/{device_id}/rules/fallback", dependencies=[Csrf])
    async def rule_fallback(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        within_h: Annotated[str, Form()],
    ):
        """The fallback rule can't be deleted, only its waiting time changes."""
        settings = await db.get(DeviceSettings, device.id)
        settings.fallback_within_min = _within_minutes(within_h)
        await db.commit()
        return await rules_response(request, db, device)

    @app.post("/devices/{device_id}/rules/{rule_id}", dependencies=[Csrf])
    async def rule_update(
        request: Request,
        db: DB,
        user: CurrentUser,
        device: OwnedDevice,
        rule_id: int,
        ready_by: Annotated[str, Form()] = "17:00",
        target: Annotated[str, Form()] = "ready_by",
        within_h: Annotated[str, Form()] = "6",
        plugged_from: Annotated[str, Form()] = "00:00",
        plugged_until: Annotated[str, Form()] = "00:00",
        wd: Annotated[list[int] | None, Form()] = None,
    ):
        rule = await db.get(DeadlineRuleRow, rule_id)
        if rule is None or rule.device_id != device.id:
            raise HTTPException(404)
        values = rule_values(target, ready_by, within_h, plugged_from, plugged_until, wd)
        for key, value in values.items():
            setattr(rule, key, value)
        await db.commit()
        return await rules_response(request, db, device)

    @app.post("/devices/{device_id}/rules/{rule_id}/delete", dependencies=[Csrf])
    async def rule_delete(
        request: Request, db: DB, user: CurrentUser, device: OwnedDevice, rule_id: int
    ):
        rule = await db.get(DeadlineRuleRow, rule_id)
        if rule is None or rule.device_id != device.id:
            raise HTTPException(404)
        await db.delete(rule)
        await db.commit()
        return await rules_response(request, db, device)

    # stats & account

    @app.get("/stats", response_class=HTMLResponse)
    async def stats(request: Request, db: DB, user: CurrentUser):
        rows = await finished_cycles(db, user.id)
        cycles = [c for c, _name in rows]
        chart_points = max(len(c["points"]) for c in cumulative(cycles)["charts"])
        tz = ZoneInfo(user.timezone)
        history = []
        for cycle, name in await recent_cycles(db, user.id):
            now = rt(request).clock_for(cycle.device_id)()
            live = (
                await live_numbers(db, cycle, rt(request).config.bidding_zone, now)
                if cycle.end_at is None
                else None
            )
            kwh = await waiting_kwh(db, cycle)
            history.append((cycle, name, cycle_chips(cycle, tz, now, live, expected_kwh=kwh)))
        return render(
            request,
            "stats.html",
            totals=totals(cycles),
            show_charts=chart_points >= 2,
            history=history,
            undo=await _undo_offer(request, db, user, tz),
            tz=tz,
            now=rt(request).clock(),
        )

    @app.post("/cycles/{cycle_id}/delete", dependencies=[Csrf])
    async def cycle_delete(request: Request, db: DB, user: CurrentUser, cycle_id: int):
        cycle = await db.get(Cycle, cycle_id)
        device = await db.get(Device, cycle.device_id) if cycle else None
        if cycle is None or device is None or device.user_id != user.id:
            raise HTTPException(404)
        row = await db.get(DeviceStateRow, device.id)
        if row is not None and row.current_cycle_id == cycle.id:
            raise HTTPException(409, "the running charge can't be deleted")
        cycle.deleted_at = utc_now()  # hidden, not gone: "undo" brings it back
        await db.commit()
        request.session["undo_cycle"] = cycle.id
        # totals and charts change too: reload the page
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    @app.post("/cycles/{cycle_id}/restore", dependencies=[Csrf])
    async def cycle_restore(request: Request, db: DB, user: CurrentUser, cycle_id: int):
        cycle = await db.get(Cycle, cycle_id)
        device = await db.get(Device, cycle.device_id) if cycle else None
        if cycle is None or device is None or device.user_id != user.id:
            raise HTTPException(404)
        cycle.deleted_at = None
        await db.commit()
        request.session.pop("undo_cycle", None)
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    @app.get("/stats.json")
    async def stats_json(request: Request, db: DB, user: CurrentUser):
        rows = await finished_cycles(db, user.id)
        payload = cumulative([c for c, _name in rows])
        payload["timezone"] = user.timezone
        return JSONResponse(payload, headers={"cache-control": "no-store"})

    # installable web app (PWA)

    @app.get("/manifest.webmanifest")
    async def manifest(request: Request):
        return JSONResponse(
            {
                "name": "efficharge",
                "short_name": "efficharge",
                "description": i18n._("Charge your battery when electricity is cheap and green."),
                "lang": i18n.get_locale(),
                "start_url": "/devices",
                "scope": "/",
                "display": "standalone",
                "background_color": "#f9f9f7",
                "theme_color": "#2a78d6",
                "icons": [
                    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {
                        "src": "/static/icons/icon-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "maskable",
                    },
                    {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    async def service_worker():
        # served from / so it may control the whole site
        return Response(
            SW_SOURCE.replace("__VERSION__", STATIC_VERSION),
            media_type="text/javascript",
            headers={"cache-control": "no-cache"},
        )

    @app.get("/impressum", response_class=HTMLResponse)
    async def impressum(request: Request):
        """Reachable without login (required by law), like the login page."""
        config = rt(request).config
        if not config.has_imprint:
            raise HTTPException(404)
        return render(request, "impressum.html", imprint=config)

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy(request: Request):
        """Reachable without login, like the imprint. German text for German, else English."""
        lang = "de" if i18n.get_locale() == "de" else "en"
        return render(request, f"privacy_{lang}.html", imprint=rt(request).config)

    @app.get("/datenschutz")
    async def datenschutz():
        return RedirectResponse("/privacy?lang=de", status_code=301)

    @app.get("/offline", response_class=HTMLResponse)
    async def offline(request: Request):
        return render(request, "offline.html")

    @app.get("/healthz")
    async def healthz(request: Request, db: DB):
        runtime = rt(request)
        await db.execute(select(1))
        fetched = runtime.fetcher.last_success if runtime.fetcher else None
        return {
            "ok": True,
            "runners": len(runtime.supervisor.runners) if runtime.supervisor else 0,
            "energy_data_age_s": (utc_now() - fetched).total_seconds() if fetched else None,
            "rss_mb": _rss_mb(),
        }


def _static_version() -> str:
    """Hash of all static files: the service worker cache changes when any of them does."""
    digest = hashlib.sha256()
    for path in sorted((HERE / "static").rglob("*")):
        if path.is_file() and path.name != "sw.js":
            digest.update(path.relative_to(HERE).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


SW_SOURCE = (HERE / "static" / "sw.js").read_text()
STATIC_VERSION = _static_version()


async def _refresh(request: Request, device_id: int, timeout_s: float = 5.0) -> None:
    """Read the plug now and wait (briefly) until the new reading is stored."""
    sup = rt(request).supervisor

    async def last_seen():
        async with rt(request).readers() as s:
            row = await s.get(DeviceStateRow, device_id)
            return row.last_seen if row else None

    before = await last_seen()
    sup.send(device_id, PollNow())
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        if await last_seen() != before:
            return


async def _undo_offer(request: Request, db: AsyncSession, user: User, tz: ZoneInfo):
    """The charge deleted last (if it is still hidden): offer to bring it back."""
    cycle_id = request.session.get("undo_cycle")
    cycle = await db.get(Cycle, cycle_id) if cycle_id else None
    if cycle is None or cycle.deleted_at is None:
        request.session.pop("undo_cycle", None)
        return None
    device = await db.get(Device, cycle.device_id)
    if device is None or device.user_id != user.id:
        return None
    return {"id": cycle.id, "when": i18n.fmt_day_time(cycle.plugged_at, tz)}


async def _status_after(request: Request, device: Device, user: User) -> HTMLResponse:
    """Let the runner handle the event, then render from a fresh session (new state)."""
    await asyncio.sleep(0.3)
    async with rt(request).readers() as fresh:
        current = await fresh.get(Device, device.id)
        return render(request, "_status.html", **await device_view(request, fresh, current, user))


def _client_ip(request: Request) -> str:
    """The visitor's address (behind the proxy: from X-Forwarded-For, see cli.serve)."""
    return request.client.host if request.client else "?"


def _end_session(request: Request) -> None:
    """Log out: an empty session, only the chosen language stays."""
    locale = request.session.get("locale")
    request.session.clear()
    if locale:
        request.session["locale"] = locale


def _safe_next(target: str | None, default: str) -> str:
    """Only local paths: no open redirects (``//evil.example``, ``/\\evil``, ``https://…``)."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return default


def _ready_days(now: datetime, tz: ZoneInfo) -> list[tuple[str, str]]:
    """Today, tomorrow, the day after: (ISO date, label) in the user's time zone."""
    today = now.astimezone(tz).date()
    labels = [i18n._("today"), i18n._("tomorrow"), None]
    out = []
    for offset, label in enumerate(labels):
        day = today + timedelta(days=offset)
        name = i18n.weekday_names()[day.weekday()]
        out.append((day.isoformat(), f"{label} ({name})" if label else name))
    return out


def _ready_default(state, now: datetime, tz: ZoneInfo) -> tuple[str, str]:
    """Pre-select the current deadline (or tomorrow 07:00), rounded to 15 minutes."""
    when = state.plan.deadline if state.plan else None
    if when is None:
        local = (now.astimezone(tz) + timedelta(days=1)).replace(hour=7, minute=0)
    else:
        local = when.astimezone(tz)
    minute = local.minute - local.minute % 15
    return local.date().isoformat(), f"{local.hour:02d}:{minute:02d}"


def _overlap_texts(rules) -> list[tuple[int, int, str]]:
    return [
        (a, b, i18n.weekdays_text(sum(1 << d for d in days))) for a, b, days in rule_overlaps(rules)
    ]


#: choices for "ready within N h" (rules and the fallback rule)
WITHIN_HOURS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 24, 36, 48)


def _within_minutes(hours: str) -> int:
    try:
        value = int(hours)
    except ValueError:
        raise HTTPException(400, "hours must be a number") from None
    if value not in WITHIN_HOURS:
        raise HTTPException(400, "unsupported number of hours")
    return value * 60


def _number(text: str) -> float | None:
    """'0,5' or '504' → float; empty or invalid → None."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def battery_capacity_kwh(wh: str, volts: str, amp_hours: str) -> float | None:
    """From V × Ah (as printed on e-bike batteries) or from Wh; stored in kWh."""
    v, ah = _number(volts), _number(amp_hours)
    if v is not None and ah is not None:
        return round(v * ah / 1000, 4)
    value = _number(wh)
    return round(value / 1000, 4) if value is not None else None


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def _planned(db: AsyncSession, device_id: int) -> dict:
    """'Currently planned': the same duration the planner uses, and from how many charges."""
    return {
        "planned_duration": (await planner_inputs(db, device_id)).duration,
        "learned_from": min(len(await past_durations(db, device_id)), LAST_N),
    }


def _priority_name(weight: float) -> str:
    """'eco' | 'balanced' | 'money' for the presets, else 'custom'."""
    for priority, w in PRESETS.items():
        if abs(w.price - weight) < 1e-6:
            return priority.value
    return "custom"


async def _removable(request: Request, db: AsyncSession, device: Device) -> bool:
    """Real plugs can be removed one by one; a virtual plug belongs to the account."""
    if request.session.get("demo"):
        return False
    account = await db.get(DeviceAccount, device.account_id) if device.account_id else None
    return account is None or account.kind != "simulated"


def _no_demo_edits(request: Request) -> None:
    if request.session.get("demo"):
        raise HTTPException(403, "Demo accounts have no account settings.")


async def _apply_settings(request: Request, device_id: int) -> None:
    sup = rt(request).supervisor
    if sup is not None and device_id in sup.runners:
        await sup.reload_device(device_id)
        sup.send(device_id, Replan())


def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None
