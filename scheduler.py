from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from models import (
    BeddingItem,
    User,
    WashLog,
    calculate_average_gap_days,
    db,
    determine_tier,
    get_primary_item,
)
from sms import (
    build_day_of_message,
    build_evening_message,
    build_monthly_summary,
    build_nextday_message,
    send_sms_message,
)


logger = logging.getLogger(__name__)
scheduler: BackgroundScheduler | None = None
MORNING_HOUR = 9
EVENING_HOUR = 18
SUMMARY_HOUR = 20


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_timezone(user: User) -> ZoneInfo:
    return ZoneInfo(user.timezone or "America/New_York")


def get_user_local_now(user: User, base_utc: datetime | None = None) -> datetime:
    base = (base_utc or utcnow()).replace(tzinfo=timezone.utc)
    return base.astimezone(get_timezone(user))


def get_user_local_date(user: User, base_utc: datetime | None = None) -> date:
    return get_user_local_now(user, base_utc).date()


def is_last_day_of_month(target_date: date) -> bool:
    return target_date.day == calendar.monthrange(target_date.year, target_date.month)[1]


def compute_next_reminder_date(
    user: User,
    cadence_days: int,
    *,
    last_washed: date | None,
    unknown_last_washed: bool = False,
    base_utc: datetime | None = None,
) -> date:
    local_now = get_user_local_now(user, base_utc)
    candidate = local_now.date() if unknown_last_washed or last_washed is None else last_washed + timedelta(days=cadence_days)

    if candidate < local_now.date():
        candidate = local_now.date()
    if candidate == local_now.date() and local_now.hour >= MORNING_HOUR:
        candidate = local_now.date() + timedelta(days=1)
    return candidate


def clear_cycle_notification_state(item: BeddingItem) -> None:
    item.day_of_sent_for = None
    item.evening_followup_sent_for = None
    item.nextday_followup_sent_for = None


def log_wash(
    item: BeddingItem,
    *,
    source: str,
    washed_at: datetime | None = None,
) -> WashLog:
    washed_utc = washed_at or utcnow()
    user = item.user
    local_wash_date = washed_utc.replace(tzinfo=timezone.utc).astimezone(get_timezone(user)).date()
    due_date = item.next_reminder
    was_on_time = local_wash_date <= (due_date + timedelta(days=1))

    log = WashLog(
        bedding_id=item.id,
        washed_at=washed_utc,
        due_date=due_date,
        was_on_time=was_on_time,
        source=source,
    )
    item.last_washed = local_wash_date
    item.next_reminder = compute_next_reminder_date(
        user,
        item.cadence_days,
        last_washed=local_wash_date,
        base_utc=washed_utc,
    )
    item.skips_remaining = 2
    clear_cycle_notification_state(item)
    db.session.add(log)
    return log


def format_date_label(target_date: date, timezone_name: str, *, reference_date: date | None = None) -> str:
    tz = ZoneInfo(timezone_name or "America/New_York")
    today = reference_date or datetime.now(tz).date()
    if target_date == today:
        return "today"
    if target_date == today + timedelta(days=1):
        return "tomorrow"
    return target_date.strftime("%A, %B %-d")


def find_most_recent_due_item(user: User) -> BeddingItem | None:
    local_today = get_user_local_date(user)
    return (
        BeddingItem.query.filter_by(user_id=user.id, active=True)
        .filter(
            (BeddingItem.next_reminder <= local_today)
            | (BeddingItem.day_of_sent_for.isnot(None))
            | (BeddingItem.evening_followup_sent_for.isnot(None))
            | (BeddingItem.nextday_followup_sent_for.isnot(None))
        )
        .order_by(BeddingItem.next_reminder.asc(), BeddingItem.id.asc())
        .first()
    )


def _days_since_last_wash(item: BeddingItem, local_today: date) -> int:
    if item.last_washed is None:
        return item.cadence_days
    return max((local_today - item.last_washed).days, item.cadence_days)


def send_morning_reminders(app, now_utc: datetime | None = None) -> dict[str, int]:
    with app.app_context():
        now = now_utc or utcnow()
        sent = 0

        for user in User.query.filter_by(active=True, verified=True).all():
            local_now = get_user_local_now(user, now)
            if local_now.hour != MORNING_HOUR:
                continue

            today = local_now.date()
            due_items = (
                BeddingItem.query.filter_by(user_id=user.id, active=True, next_reminder=today)
                .order_by(BeddingItem.id.asc())
                .all()
            )
            for item in due_items:
                if item.day_of_sent_for == today:
                    continue
                body = build_day_of_message(
                    item_type=item.item_type,
                    days_since=_days_since_last_wash(item, today),
                    seed=(item.id * 19) + today.toordinal(),
                )
                send_sms_message(user.phone, body)
                item.day_of_sent_for = today
                sent += 1

        db.session.commit()
        return {"sent": sent}


def send_evening_followups(app, now_utc: datetime | None = None) -> dict[str, int]:
    with app.app_context():
        now = now_utc or utcnow()
        sent = 0

        for user in User.query.filter_by(active=True, verified=True).all():
            local_now = get_user_local_now(user, now)
            if local_now.hour != EVENING_HOUR:
                continue

            today = local_now.date()
            items = (
                BeddingItem.query.filter_by(user_id=user.id, active=True, next_reminder=today)
                .filter(BeddingItem.day_of_sent_for == today)
                .order_by(BeddingItem.id.asc())
                .all()
            )
            for item in items:
                if item.evening_followup_sent_for == today:
                    continue
                body = build_evening_message(
                    item_type=item.item_type,
                    seed=(item.id * 23) + today.toordinal(),
                )
                send_sms_message(user.phone, body)
                item.evening_followup_sent_for = today
                sent += 1

        db.session.commit()
        return {"sent": sent}


def send_nextday_followups(app, now_utc: datetime | None = None) -> dict[str, int]:
    with app.app_context():
        now = now_utc or utcnow()
        sent = 0

        for user in User.query.filter_by(active=True, verified=True).all():
            local_now = get_user_local_now(user, now)
            if local_now.hour != MORNING_HOUR:
                continue

            today = local_now.date()
            overdue_date = today - timedelta(days=1)
            items = (
                BeddingItem.query.filter_by(user_id=user.id, active=True, next_reminder=overdue_date)
                .filter(BeddingItem.day_of_sent_for == overdue_date)
                .order_by(BeddingItem.id.asc())
                .all()
            )
            for item in items:
                if item.nextday_followup_sent_for is not None:
                    continue
                body = build_nextday_message(
                    item_type=item.item_type,
                    seed=(item.id * 29) + overdue_date.toordinal(),
                )
                send_sms_message(user.phone, body)
                item.nextday_followup_sent_for = today
                sent += 1

        db.session.commit()
        return {"sent": sent}


def send_monthly_summary(app, now_utc: datetime | None = None) -> dict[str, int]:
    with app.app_context():
        now = now_utc or utcnow()
        sent = 0

        for user in User.query.filter_by(active=True, verified=True).all():
            local_now = get_user_local_now(user, now)
            local_today = local_now.date()
            if local_now.hour != SUMMARY_HOUR or not is_last_day_of_month(local_today):
                continue
            if user.last_monthly_summary_for == local_today:
                continue

            primary_item = get_primary_item(user.id)
            if primary_item is None:
                continue

            month_start = local_today.replace(day=1)
            month_start_local = datetime.combine(month_start, time.min, tzinfo=get_timezone(user))
            next_day_local = datetime.combine(
                local_today + timedelta(days=1),
                time.min,
                tzinfo=get_timezone(user),
            )
            month_start_utc = month_start_local.astimezone(timezone.utc).replace(tzinfo=None)
            next_day_utc = next_day_local.astimezone(timezone.utc).replace(tzinfo=None)
            logs = (
                WashLog.query.filter_by(bedding_id=primary_item.id)
                .filter(WashLog.washed_at >= month_start_utc, WashLog.washed_at < next_day_utc)
                .order_by(WashLog.washed_at.asc())
                .all()
            )
            avg_gap_days = calculate_average_gap_days(primary_item, user.timezone)
            tier_name = determine_tier(avg_gap_days)["name"]
            body = build_monthly_summary(
                month_name=local_today.strftime("%B"),
                washes=len(logs),
                average_gap_days=avg_gap_days,
                tier_name=tier_name,
            )
            send_sms_message(user.phone, body)
            user.last_monthly_summary_for = local_today
            sent += 1

        db.session.commit()
        return {"sent": sent}


def init_scheduler(app) -> BackgroundScheduler:
    global scheduler
    if scheduler and scheduler.running:
        return scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        send_morning_reminders,
        "cron",
        minute=0,
        id="send_morning_reminders",
        args=[app],
        replace_existing=True,
    )
    scheduler.add_job(
        send_evening_followups,
        "cron",
        minute=0,
        id="send_evening_followups",
        args=[app],
        replace_existing=True,
    )
    scheduler.add_job(
        send_nextday_followups,
        "cron",
        minute=0,
        id="send_nextday_followups",
        args=[app],
        replace_existing=True,
    )
    scheduler.add_job(
        send_monthly_summary,
        "cron",
        minute=0,
        id="send_monthly_summary",
        args=[app],
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Fresh Sheets scheduler started.")
    return scheduler
