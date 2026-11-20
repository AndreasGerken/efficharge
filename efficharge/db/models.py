"""Database models (SQLAlchemy 2.0). All timestamps are UTC."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """Stores naive UTC, returns aware UTC. SQLite has no time zones; this makes
    naive datetimes impossible to get out of the DB, and refuses to store them."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(UTC)


# Stable constraint names, so Alembic migrations work on SQLite (batch mode).
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None for the demo user
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Berlin")
    locale: Mapped[str] = mapped_column(String(8), default="de")
    #: not used (yet): no setting for it any more, kept to spare a migration
    tariff: Mapped[str] = mapped_column(String(16), default="dynamic")  # dynamic | fixed
    #: what matters more, for all plugs: 0 = only green power, 1 = only price.
    #: Presets: eco 0.2, balanced 0.5, money 0.8 (planner.window.PRESETS)
    price_weight: Mapped[float] = mapped_column(Float, default=0.5, server_default="0.5")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    #: registrations wait for the admin (``efficharge approve``); None: not accepted yet
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: "who are you", written at registration for the admin
    about: Mapped[str | None] = mapped_column(Text)

    # passive_deletes: let the DB's ON DELETE CASCADE remove children (deleting a user
    # deletes their accounts, devices, cycles, samples) instead of nulling foreign keys
    accounts: Mapped[list[DeviceAccount]] = relationship(
        back_populates="user", passive_deletes=True
    )
    devices: Mapped[list[Device]] = relationship(back_populates="user", passive_deletes=True)


class DeviceAccount(Base):
    """A user's account at a vendor (Meross, Shelly, …). Holds the encrypted token."""

    __tablename__ = "device_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    #: efficharge.crypto format; bound to this row's id + kind
    auth_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | needs_reauth
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="accounts")
    devices: Mapped[list[Device]] = relationship(back_populates="account", passive_deletes=True)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: None when a home system switches the plug itself and only asks us (planned API)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_accounts.id", ondelete="CASCADE"), index=True
    )
    vendor_device_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(1), default="A")  # A = we switch, B = API
    capabilities: Mapped[int] = mapped_column(Integer, default=0)  # devices.base.Capability
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="devices")
    account: Mapped[DeviceAccount | None] = relationship(back_populates="devices")
    settings: Mapped[DeviceSettings] = relationship(
        back_populates="device", cascade="all, delete-orphan", uselist=False
    )
    rules: Mapped[list[DeadlineRuleRow]] = relationship(
        back_populates="device", cascade="all, delete-orphan", order_by="DeadlineRuleRow.position"
    )


class DeviceSettings(Base):
    __tablename__ = "device_settings"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    threshold_w: Mapped[float] = mapped_column(Float, default=2.0)
    standby_w: Mapped[float | None] = mapped_column(Float)  # learned
    default_duration_min: Mapped[int] = mapped_column(Integer, default=180)
    battery_kwh: Mapped[float | None] = mapped_column(Float)  # for unmetered savings estimates
    metered: Mapped[bool] = mapped_column(Boolean, default=True)
    #: the fallback rule (always there, can't be deleted): ready within this long
    fallback_within_min: Mapped[int] = mapped_column(Integer, default=720, server_default="720")

    device: Mapped[Device] = relationship(back_populates="settings")


class DeadlineRuleRow(Base):
    __tablename__ = "deadline_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    weekdays: Mapped[int] = mapped_column(Integer, default=0b1111111)  # bit 0 = Monday
    plugged_from: Mapped[str] = mapped_column(String(5), default="00:00")  # local HH:MM
    plugged_until: Mapped[str] = mapped_column(String(5), default="00:00")
    #: one of the two: "ready by HH:MM" (local) or "ready within N minutes after plug-in"
    ready_by: Mapped[str | None] = mapped_column(String(5))
    within_min: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)

    device: Mapped[Device] = relationship(back_populates="rules")


class DeviceStateRow(Base):
    """Persisted control.machine.DeviceState (JSON) + a few columns for queries/UI."""

    __tablename__ = "device_state"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    phase: Mapped[str] = mapped_column(String(20), default="idle")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    current_cycle_id: Mapped[int | None] = mapped_column(
        ForeignKey("cycles.id", ondelete="SET NULL")
    )
    should_charge_now: Mapped[bool] = mapped_column(Boolean, default=True)  # for the planned API
    last_power_w: Mapped[float | None] = mapped_column(Float)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Cycle(Base):
    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    plugged_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    planned_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    planned_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    plan: Mapped[dict | None] = mapped_column(JSON)
    actual_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    end_reason: Mapped[str | None] = mapped_column(String(16))  # full|unplugged|timer
    override: Mapped[str | None] = mapped_column(String(16))  # last user override, if any
    override_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    kwh: Mapped[float | None] = mapped_column(Float)
    cost_actual_eur: Mapped[float | None] = mapped_column(Float)  # spot price part only
    cost_baseline_eur: Mapped[float | None] = mapped_column(Float)
    co2_actual_g: Mapped[float | None] = mapped_column(Float)
    co2_baseline_g: Mapped[float | None] = mapped_column(Float)
    ren_actual_pct: Mapped[float | None] = mapped_column(Float)  # green-power share, %
    ren_baseline_pct: Mapped[float | None] = mapped_column(Float)
    finalised: Mapped[bool] = mapped_column(Boolean, default=False)
    #: hidden by the user ("delete" in the list of charges); can be undone
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_cycles_device_plugged", "device_id", "plugged_at"),)


class PowerSampleRow(Base):
    __tablename__ = "power_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    watts: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_power_samples_device_ts", "device_id", "ts"),)


class EnergyDataRow(Base):
    """One 15-min slot of market + grid data, shared by all users."""

    __tablename__ = "energy_data"

    zone: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    price_eur_mwh: Mapped[float | None] = mapped_column(Float)
    ren_share_pct: Mapped[float | None] = mapped_column(Float)
    co2_g_kwh: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex; the token is shown once
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("token_hash"),)
