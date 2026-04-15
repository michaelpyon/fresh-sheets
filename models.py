from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column, relationship


db = SQLAlchemy()


DEFAULT_CADENCE_DAYS = {
    "sheets": 7,
    "pillowcases": 4,
    "duvet_cover": 30,
    "mattress_protector": 90,
}

ITEM_LABELS = {
    "sheets": "Sheets",
    "pillowcases": "Pillowcases",
    "duvet_cover": "Duvet Cover",
    "mattress_protector": "Mattress Protector",
}

RECOMMENDED_CADENCE_TEXT = {
    "sheets": "Weekly",
    "pillowcases": "Every 3-4 days",
    "duvet_cover": "Monthly",
    "mattress_protector": "Quarterly",
}

EDITABLE_CADENCE_OPTIONS = {
    "sheets": [7, 14, 30],
    "pillowcases": [3, 4, 7],
    "duvet_cover": [14, 30, 45],
    "mattress_protector": [60, 90, 120],
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_item_label(item_type: str) -> str:
    return ITEM_LABELS.get(item_type, item_type.replace("_", " ").title())


def default_cadence_for(item_type: str) -> int:
    return DEFAULT_CADENCE_DAYS[item_type]


def recommended_cadence_for(item_type: str) -> str:
    return RECOMMENDED_CADENCE_TEXT[item_type]


def editable_cadence_options_for(item_type: str) -> list[int]:
    return EDITABLE_CADENCE_OPTIONS[item_type]


class User(db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(db.String(20), unique=True, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(db.String(50), default="America/New_York", nullable=False)
    verified: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    verify_code: Mapped[str | None] = mapped_column(db.String(6))
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    last_monthly_summary_for: Mapped[date | None] = mapped_column(db.Date)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    bedding_items: Mapped[list["BeddingItem"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="BeddingItem.id",
    )


class BeddingItem(db.Model):
    __tablename__ = "bedding_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(db.ForeignKey("users.id"), nullable=False, index=True)
    item_type: Mapped[str] = mapped_column(db.String(30), nullable=False)
    cadence_days: Mapped[int] = mapped_column(db.Integer, nullable=False)
    last_washed: Mapped[date | None] = mapped_column(db.Date)
    next_reminder: Mapped[date] = mapped_column(db.Date, nullable=False, index=True)
    skips_remaining: Mapped[int] = mapped_column(db.Integer, default=2, nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    day_of_sent_for: Mapped[date | None] = mapped_column(db.Date)
    evening_followup_sent_for: Mapped[date | None] = mapped_column(db.Date)
    nextday_followup_sent_for: Mapped[date | None] = mapped_column(db.Date)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="bedding_items")
    wash_logs: Mapped[list["WashLog"]] = relationship(
        back_populates="bedding_item",
        cascade="all, delete-orphan",
        order_by="WashLog.washed_at.desc()",
    )


class WashLog(db.Model):
    __tablename__ = "wash_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    bedding_id: Mapped[int] = mapped_column(db.ForeignKey("bedding_items.id"), nullable=False, index=True)
    washed_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, nullable=False)
    due_date: Mapped[date | None] = mapped_column(db.Date)
    was_on_time: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    source: Mapped[str | None] = mapped_column(db.String(20))

    bedding_item: Mapped[BeddingItem] = relationship(back_populates="wash_logs")


def get_primary_item(user_id: int) -> BeddingItem | None:
    return BeddingItem.query.filter_by(user_id=user_id, item_type="sheets", active=True).one_or_none()


def calculate_streak(user_id: int) -> int:
    primary_item = get_primary_item(user_id)
    if primary_item is None:
        return 0

    logs = (
        WashLog.query.filter_by(bedding_id=primary_item.id)
        .order_by(WashLog.washed_at.desc())
        .all()
    )
    streak = 0
    for log in logs:
        if not log.was_on_time:
            break
        streak += 1
    return streak


def recent_wash_dates_for_item(
    bedding_id: int,
    timezone_name: str,
    *,
    days: int = 7,
    reference_date: date | None = None,
) -> set[date]:
    tz = ZoneInfo(timezone_name or "America/New_York")
    local_today = reference_date or datetime.now(tz).date()
    start_date = local_today - timedelta(days=days - 1)

    rows = db.session.query(WashLog.washed_at).filter(WashLog.bedding_id == bedding_id).all()
    return {
        local_date
        for (washed_at,) in rows
        if washed_at is not None
        for local_date in [washed_at.replace(tzinfo=timezone.utc).astimezone(tz).date()]
        if start_date <= local_date <= local_today
    }


def get_wash_history_for_item(
    bedding_id: int,
    timezone_name: str,
) -> list[date]:
    tz = ZoneInfo(timezone_name or "America/New_York")
    rows = (
        db.session.query(WashLog.washed_at)
        .filter(WashLog.bedding_id == bedding_id)
        .order_by(WashLog.washed_at.desc())
        .all()
    )
    return [
        washed_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
        for (washed_at,) in rows
        if washed_at is not None
    ]


def calculate_average_gap_days(item: BeddingItem, timezone_name: str) -> float:
    history = list(reversed(get_wash_history_for_item(item.id, timezone_name)))
    if len(history) < 2:
        return float(item.cadence_days)

    gaps = [
        (current_date - previous_date).days
        for previous_date, current_date in zip(history, history[1:])
        if current_date > previous_date
    ]
    if not gaps:
        return float(item.cadence_days)
    return round(float(mean(gaps)), 1)


def determine_tier(avg_gap_days: float) -> dict[str, str]:
    if avg_gap_days <= 8:
        return {
            "name": "Fresh Prince",
            "tone": "blue",
            "note": "Clean-sleep royalty.",
        }
    if avg_gap_days <= 15:
        return {
            "name": "Acceptable Adult",
            "tone": "gray",
            "note": "Functional. Respectable. Mostly fresh.",
        }
    return {
        "name": "Feral",
        "tone": "red",
        "note": "no judgment. okay, a little.",
    }
