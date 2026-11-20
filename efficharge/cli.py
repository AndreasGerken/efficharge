"""Command line: ``efficharge <command>``."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path


async def _run_workers() -> None:
    """Fetcher + supervisor without the web app (until slice 4 adds it)."""
    from efficharge.config import Config
    from efficharge.control.supervisor import Supervisor
    from efficharge.crypto import Vault
    from efficharge.db.session import make_engine, make_sessionmaker
    from efficharge.energy.client import EnergyChartsClient
    from efficharge.energy.fetcher import EnergyFetcher

    cfg = Config.from_env()
    sessions = make_sessionmaker(make_engine(cfg.database_url))

    def clock() -> datetime:
        return datetime.now(UTC)

    sup = Supervisor(
        sessions,
        Vault.from_config(cfg.data_keys),
        zone=cfg.bidding_zone,
        clock=clock,
        poll_interval=cfg.poll_interval,
    )
    fetcher = EnergyFetcher(
        EnergyChartsClient(bidding_zone=cfg.bidding_zone, country=cfg.country),
        sessions,
        cfg.bidding_zone,
        clock,
        on_new_prices=sup.broadcast_replan,
        interval=cfg.fetch_interval,
    )
    await fetcher.fetch_once()
    await sup.start()
    try:
        await fetcher.run()
    finally:
        await sup.stop()


DEV_SECRETS = Path(".efficharge-dev.env")


def _load_dev_secrets(new_data_key, new_secret) -> None:
    """Dev keys must survive restarts, or stored plug tokens can't be decrypted anymore."""
    import os

    if not DEV_SECRETS.exists():
        DEV_SECRETS.write_text(
            f"EFFICHARGE_DATA_KEYS=dev:{new_data_key()}\nEFFICHARGE_SECRET_KEY={new_secret(48)}\n"
        )
        DEV_SECRETS.chmod(0o600)
    for line in DEV_SECRETS.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.startswith("EFFICHARGE_") and value:
            os.environ.setdefault(key, value)


def _serve(host: str, port: int, dev: bool, speed: float) -> None:
    import os
    import secrets
    from dataclasses import replace

    import uvicorn

    from efficharge.config import Config
    from efficharge.crypto import generate_key
    from efficharge.db.migrate import upgrade
    from efficharge.web.app import create_app

    if dev:  # local keys (kept in a git-ignored file) and DB, http cookies allowed
        _load_dev_secrets(generate_key, secrets.token_urlsafe)
        os.environ.setdefault("EFFICHARGE_DATABASE_URL", "sqlite+aiosqlite:///dev.db")
    cfg = Config.from_env()
    if dev:
        cfg = replace(cfg, cookie_secure=False, registration_open=True)
    logging.basicConfig(level=logging.INFO)
    upgrade(cfg.database_url)
    clock = None
    if speed != 1:
        from efficharge.demo import FastClock

        clock = FastClock(speed)
        # poll every 30 *simulated* seconds
        cfg = replace(cfg, poll_interval=timedelta(seconds=max(0.5, 30 / speed)))
    app = create_app(cfg, **({"clock": clock} if clock else {}))
    # exactly one worker: the supervisor (one runner per plug) must exist only once
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        proxy_headers=True,
        # the reverse proxy's address (Caddy); in a container: the Docker network's gateway
        forwarded_allow_ips=os.environ.get("EFFICHARGE_TRUSTED_PROXY", "127.0.0.1"),
        server_header=False,
        # no access log: it would only collect IP addresses (like Caddy, see the privacy page)
        access_log=False,
    )


USER_COMMANDS = ("add-user", "set-password", "pending", "approve", "reject", "add-virtual-plug")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="efficharge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gen-key", help="print a new data key for EFFICHARGE_DATA_KEYS")
    sub.add_parser("migrate", help="create/upgrade the database schema")
    sub.add_parser("run", help="run energy fetcher + device supervisor (no web)")
    serve = sub.add_parser("serve", help="web app + workers in one process")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--dev", action="store_true", help="local development settings")
    serve.add_argument("--speed", type=float, default=1, help="simulated time speed (demo)")
    check = sub.add_parser("meross-check", help="read-only check of a stored Meross account")
    check.add_argument("--dev", action="store_true", help="use the local dev database and keys")
    for name, text in (
        ("add-user", "create a login with a virtual plug (asks for the password)"),
        ("set-password", "change a password (asks for it)"),
        ("approve", "accept a registration"),
        ("reject", "delete a waiting registration"),
        ("add-virtual-plug", "give a user another virtual plug"),
    ):
        user_cmd = sub.add_parser(name, help=text)
        user_cmd.add_argument("email")
        user_cmd.add_argument("--locale", default="de", choices=["de", "en"])
    sub.add_parser("pending", help="registrations waiting for approval")
    example = sub.add_parser("make-example", help="rebuild the start page's example charge")
    example.add_argument("day", help="a past day, e.g. 2026-04-17")
    example.add_argument("--plugged", default="09:00", help="plug-in time (Europe/Berlin)")
    example.add_argument("--ready", default="18:00", help="ready-by time (Europe/Berlin)")
    demo = sub.add_parser("demo", help="simulated plug + real prices, sped up, in the terminal")
    demo.add_argument("--speed", type=float, default=600, help="simulated seconds per second")
    demo.add_argument("--hours", type=float, default=30, help="simulated hours")
    demo.add_argument("--db", type=Path, default=Path("demo.db"))
    args = parser.parse_args(argv)

    if args.command == "gen-key":
        from efficharge.crypto import generate_key

        print(f"k{datetime.now(UTC):%Y%m%d}:{generate_key()}")
    elif args.command == "migrate":
        from efficharge.config import Config
        from efficharge.db.migrate import upgrade

        upgrade(Config.from_env().database_url)
    elif args.command == "run":
        from efficharge.config import Config
        from efficharge.db.migrate import upgrade

        logging.basicConfig(level=logging.INFO)
        upgrade(Config.from_env().database_url)
        asyncio.run(_run_workers())
    elif args.command == "make-example":
        from efficharge.web.example_build import main as make_example

        logging.basicConfig(level=logging.WARNING)
        asyncio.run(make_example(args.day, args.plugged, args.ready))
    elif args.command == "serve":
        _serve(args.host, args.port, args.dev, args.speed)
    elif args.command == "meross-check":
        import os

        from efficharge.devices.meross_check import main as check_main

        if args.dev:
            if not DEV_SECRETS.exists():
                raise SystemExit("No dev keys yet: start `efficharge serve --dev` once.")
            _load_dev_secrets(lambda: "", lambda n: "")
            os.environ.setdefault("EFFICHARGE_DATABASE_URL", "sqlite+aiosqlite:///dev.db")
        check_main(
            os.environ.get("EFFICHARGE_DATABASE_URL", "sqlite+aiosqlite:///efficharge.db"),
            os.environ["EFFICHARGE_DATA_KEYS"],
        )
    elif args.command in USER_COMMANDS:
        import os

        from efficharge.crypto import Vault
        from efficharge.db.migrate import upgrade
        from efficharge.users_cli import run as users_run

        url = os.environ.get("EFFICHARGE_DATABASE_URL", "sqlite+aiosqlite:///efficharge.db")
        upgrade(url)
        users_run(
            args.command,
            url,
            Vault.from_config(os.environ.get("EFFICHARGE_DATA_KEYS", "")),
            getattr(args, "email", None),
            timezone=os.environ.get("EFFICHARGE_TIMEZONE", "Europe/Berlin"),
            locale=getattr(args, "locale", "de"),
        )
    elif args.command == "demo":
        from efficharge.demo import main as demo_main

        demo_main(args.db, args.speed, args.hours)


if __name__ == "__main__":
    main()
