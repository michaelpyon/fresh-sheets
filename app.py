from __future__ import annotations

import os
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from phonenumbers.phonenumberutil import NumberParseException
from twilio.twiml.messaging_response import MessagingResponse

from models import (
    BeddingItem,
    DEFAULT_CADENCE_DAYS,
    User,
    WashLog,
    calculate_average_gap_days,
    calculate_streak,
    db,
    determine_tier,
    editable_cadence_options_for,
    get_item_label,
    get_primary_item,
    get_wash_history_for_item,
    recent_wash_dates_for_item,
    recommended_cadence_for,
)
from scheduler import (
    clear_cycle_notification_state,
    compute_next_reminder_date,
    find_most_recent_due_item,
    format_date_label,
    get_user_local_date,
    get_user_local_now,
    init_scheduler,
    log_wash,
    utcnow,
)
from sms import (
    build_completion_message,
    build_help_message,
    build_no_skips_message,
    build_skip_message,
    build_start_message,
    build_stop_message,
    build_verification_message,
    build_welcome_message,
    send_sms_message,
)


load_dotenv()


ITEM_TYPES = set(DEFAULT_CADENCE_DAYS)


def normalize_database_url(url: str | None) -> str:
    if not url:
        return "sqlite:///fresh_sheets.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def normalize_phone(raw_phone: str) -> str:
    try:
        parsed = phonenumbers.parse(raw_phone, "US")
    except NumberParseException as exc:
        raise ValueError("Please enter a valid US phone number.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Please enter a valid US phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def parse_timezone_name(value: Any) -> str:
    timezone_name = str(value or "America/New_York").strip() or "America/New_York"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Timezone is invalid.") from exc
    return timezone_name


def parse_date_value(value: Any, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date.") from exc


def parse_positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return parsed


def format_cadence_label(days: int) -> str:
    if days == 7:
        return "Weekly"
    if days == 14:
        return "Every 2 weeks"
    if days == 30:
        return "Monthly"
    if days == 90:
        return "Quarterly"
    if days == 1:
        return "Daily"
    return f"Every {days} days"


def format_last_washed_label(last_washed: date | None, local_today: date) -> str:
    if last_washed is None:
        return "Not logged yet"
    days_ago = (local_today - last_washed).days
    if days_ago <= 0:
        return "Today"
    if days_ago == 1:
        return "Yesterday"
    return f"{days_ago} days ago"


def build_week_dots(hit_dates: set[date], reference_date: date) -> list[dict[str, Any]]:
    start = reference_date - timedelta(days=6)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "label": (start + timedelta(days=index)).strftime("%a"),
            "filled": (start + timedelta(days=index)) in hit_dates,
        }
        for index in range(7)
    ]


def format_streak_display(streak: int, cadence_days: int) -> str:
    if streak <= 0:
        return "No streak yet"
    if cadence_days <= 8:
        noun = "week" if streak == 1 else "weeks"
        return f"{streak} {noun} running"
    noun = "cycle" if streak == 1 else "cycles"
    return f"{streak} {noun} on time"


def parse_setup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    timezone_name = parse_timezone_name(payload.get("timezone"))
    unknown_last_washed = bool(payload.get("unknown_last_washed", False))
    last_washed = None if unknown_last_washed else parse_date_value(payload.get("last_washed"), "Last washed")
    sheets_cadence_days = parse_positive_int(payload.get("sheets_cadence_days") or 7, "Sheets cadence")
    add_ons = payload.get("add_ons") or {}
    if not isinstance(add_ons, dict):
        raise ValueError("Optional items are invalid.")

    return {
        "timezone": timezone_name,
        "unknown_last_washed": unknown_last_washed,
        "last_washed": last_washed,
        "sheets_cadence_days": sheets_cadence_days,
        "include_pillowcases": bool(add_ons.get("pillowcases", False)),
        "include_duvet_cover": bool(add_ons.get("duvet_cover", False)),
        "include_mattress_protector": bool(add_ons.get("mattress_protector", False)),
    }


def build_item_specs(parsed_payload: dict[str, Any]) -> list[dict[str, Any]]:
    last_washed = parsed_payload["last_washed"]
    unknown_last_washed = parsed_payload["unknown_last_washed"]

    specs = [
        {
            "item_type": "sheets",
            "cadence_days": parsed_payload["sheets_cadence_days"],
            "last_washed": last_washed,
            "unknown_last_washed": unknown_last_washed,
        }
    ]
    if parsed_payload["include_pillowcases"]:
        specs.append(
            {
                "item_type": "pillowcases",
                "cadence_days": DEFAULT_CADENCE_DAYS["pillowcases"],
                "last_washed": last_washed,
                "unknown_last_washed": unknown_last_washed,
            }
        )
    if parsed_payload["include_duvet_cover"]:
        specs.append(
            {
                "item_type": "duvet_cover",
                "cadence_days": DEFAULT_CADENCE_DAYS["duvet_cover"],
                "last_washed": last_washed,
                "unknown_last_washed": unknown_last_washed,
            }
        )
    if parsed_payload["include_mattress_protector"]:
        specs.append(
            {
                "item_type": "mattress_protector",
                "cadence_days": DEFAULT_CADENCE_DAYS["mattress_protector"],
                "last_washed": last_washed,
                "unknown_last_washed": unknown_last_washed,
            }
        )
    return specs


def upsert_user_and_items(user: User, payload: dict[str, Any]) -> None:
    parsed = parse_setup_payload(payload)
    user.timezone = parsed["timezone"]
    user.active = True

    item_specs = build_item_specs(parsed)
    selected_types = {spec["item_type"] for spec in item_specs}
    existing_items = {item.item_type: item for item in user.bedding_items}

    for item_type, item in existing_items.items():
        if item_type not in selected_types:
            item.active = False

    for spec in item_specs:
        item = existing_items.get(spec["item_type"])
        if item is None:
            item = BeddingItem(
                user=user,
                item_type=spec["item_type"],
                cadence_days=spec["cadence_days"],
                next_reminder=get_user_local_date(user),
                active=True,
            )
            db.session.add(item)

        item.active = True
        item.cadence_days = spec["cadence_days"]
        item.last_washed = spec["last_washed"]
        item.next_reminder = compute_next_reminder_date(
            user,
            item.cadence_days,
            last_washed=item.last_washed,
            unknown_last_washed=spec["unknown_last_washed"],
        )
        item.skips_remaining = 2
        clear_cycle_notification_state(item)


def summarize_first_reminder(user: User) -> dict[str, Any] | None:
    active_items = [item for item in user.bedding_items if item.active]
    if not active_items:
        return None
    item = min(active_items, key=lambda current: (current.next_reminder, current.id))
    return {
        "item_id": item.id,
        "item_type": item.item_type,
        "item_label": get_item_label(item.item_type),
        "next_reminder": item.next_reminder.isoformat(),
        "next_reminder_label": format_date_label(item.next_reminder, user.timezone),
        "cadence_days": item.cadence_days,
        "cadence_label": format_cadence_label(item.cadence_days),
    }


def serialize_item(item: BeddingItem, user: User, reference_date: date) -> dict[str, Any]:
    hit_dates = recent_wash_dates_for_item(item.id, user.timezone, reference_date=reference_date)
    return {
        "id": item.id,
        "item_type": item.item_type,
        "item_label": get_item_label(item.item_type),
        "cadence_days": item.cadence_days,
        "cadence_label": format_cadence_label(item.cadence_days),
        "recommended_cadence": recommended_cadence_for(item.item_type),
        "editable_cadence_options": editable_cadence_options_for(item.item_type),
        "last_washed": item.last_washed.isoformat() if item.last_washed else None,
        "last_washed_label": format_last_washed_label(item.last_washed, reference_date),
        "next_reminder": item.next_reminder.isoformat(),
        "next_reminder_label": format_date_label(
            item.next_reminder,
            user.timezone,
            reference_date=reference_date,
        ),
        "skips_remaining": item.skips_remaining,
        "active": item.active,
        "streak_dots": build_week_dots(hit_dates, reference_date),
    }


def build_dashboard_payload(user: User) -> dict[str, Any]:
    local_today = get_user_local_date(user)
    active_items = sorted(
        [item for item in user.bedding_items if item.active],
        key=lambda current: (current.next_reminder, current.id),
    )
    primary_item = next((item for item in active_items if item.item_type == "sheets"), None)
    avg_gap_days = calculate_average_gap_days(primary_item, user.timezone) if primary_item else 0.0
    tier = determine_tier(avg_gap_days) if primary_item else determine_tier(30.0)
    streak = calculate_streak(user.id)
    history_dates = get_wash_history_for_item(primary_item.id, user.timezone)[:8] if primary_item else []

    return {
        "phone": user.phone,
        "timezone": user.timezone,
        "streak": streak,
        "streak_display": format_streak_display(streak, primary_item.cadence_days if primary_item else 7),
        "tier": tier,
        "average_gap_days": avg_gap_days,
        "history": [
            {
                "date": entry.isoformat(),
                "label": entry.strftime("%b %-d, %Y"),
            }
            for entry in history_dates
        ],
        "items": [serialize_item(item, user, local_today) for item in active_items],
        "first_reminder": summarize_first_reminder(user),
    }


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "fresh-sheets-dev-secret"),
        SQLALCHEMY_DATABASE_URI=normalize_database_url(os.getenv("DATABASE_URL")),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        JSON_SORT_KEYS=False,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    CORS(app, resources={r"/api/*": {"origins": "*"}, r"/sms/*": {"origins": "*"}})
    db.init_app(app)

    with app.app_context():
        db.create_all()

    @app.get("/")
    def index():
        return send_from_directory(Path(app.root_path) / "frontend", "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/api/setup")
    def setup():
        payload = request.get_json(silent=True) or {}
        try:
            phone = normalize_phone(str(payload.get("phone", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        user = User.query.filter_by(phone=phone).one_or_none()
        is_new_user = user is None
        if user is None:
            user = User(phone=phone)
            db.session.add(user)

        try:
            user.phone = phone
            upsert_user_and_items(user, payload)
            db.session.flush()
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400

        response = {
            "phone": user.phone,
            "verification_required": not user.verified,
            "first_reminder": summarize_first_reminder(user),
        }

        if user.verified:
            db.session.commit()
            response["dashboard"] = build_dashboard_payload(user)
            return jsonify(response), 201 if is_new_user else 200

        verify_code = f"{random.randint(0, 999999):06d}"
        user.verify_code = verify_code
        user.verified = False
        db.session.commit()
        send_sms_message(user.phone, build_verification_message(verify_code))
        if os.getenv("EXPOSE_VERIFY_CODE", "0") == "1":
            response["debug_verify_code"] = verify_code
        return jsonify(response), 201 if is_new_user else 200

    @app.post("/api/verify")
    def verify():
        payload = request.get_json(silent=True) or {}
        try:
            phone = normalize_phone(str(payload.get("phone", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        user = User.query.filter_by(phone=phone).one_or_none()
        if user is None:
            return jsonify({"error": "No Fresh Sheets account found for that phone."}), 404

        code = str(payload.get("code", "")).strip()
        if user.verify_code != code:
            return jsonify({"error": "That verification code does not match."}), 400

        user.verified = True
        user.active = True
        user.verify_code = None
        db.session.commit()

        first_reminder = summarize_first_reminder(user)
        if first_reminder is not None:
            send_sms_message(user.phone, build_welcome_message(first_reminder["next_reminder_label"]))

        return jsonify(
            {
                "confirmed_message": (
                    f"Your next sheet day: {first_reminder['next_reminder_label']}."
                    if first_reminder
                    else "You're set."
                ),
                "first_reminder": first_reminder,
                "dashboard": build_dashboard_payload(user),
            }
        )

    @app.get("/api/dashboard/<path:phone_token>")
    def dashboard(phone_token: str):
        try:
            phone = normalize_phone(unquote(phone_token))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        user = User.query.filter_by(phone=phone, verified=True).one_or_none()
        if user is None:
            return jsonify({"error": "No verified Fresh Sheets account found."}), 404
        return jsonify(build_dashboard_payload(user))

    @app.post("/api/update-cadence")
    def update_cadence():
        payload = request.get_json(silent=True) or {}
        try:
            phone = normalize_phone(str(payload.get("phone", "")))
            cadence_days = parse_positive_int(payload.get("cadence_days"), "Cadence")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        item_id = payload.get("item_id")
        user = User.query.filter_by(phone=phone, verified=True).one_or_none()
        if user is None:
            return jsonify({"error": "No verified Fresh Sheets account found."}), 404

        item = BeddingItem.query.filter_by(id=item_id, user_id=user.id, active=True).one_or_none()
        if item is None:
            return jsonify({"error": "That bedding item was not found."}), 404

        item.cadence_days = cadence_days
        item.next_reminder = compute_next_reminder_date(
            user,
            cadence_days,
            last_washed=item.last_washed,
            unknown_last_washed=item.last_washed is None,
        )
        item.skips_remaining = 2
        clear_cycle_notification_state(item)
        db.session.commit()
        return jsonify(build_dashboard_payload(user))

    @app.post("/sms/inbound")
    def inbound_sms():
        response = MessagingResponse()
        command = request.form.get("Body", "").strip().upper()
        raw_phone = request.form.get("From", "")

        try:
            phone = normalize_phone(raw_phone)
        except ValueError:
            response.message(build_help_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        user = User.query.filter_by(phone=phone).one_or_none()
        if user is None:
            response.message("You are not on Fresh Sheets yet. Sign up first, then reply here.")
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "HELP":
            response.message(build_help_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "STOP":
            user.active = False
            for item in user.bedding_items:
                item.active = False
            db.session.commit()
            response.message(build_stop_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "START":
            user.active = True
            for item in user.bedding_items:
                item.active = True
                item.next_reminder = compute_next_reminder_date(
                    user,
                    item.cadence_days,
                    last_washed=item.last_washed,
                    unknown_last_washed=item.last_washed is None,
                )
                clear_cycle_notification_state(item)
            db.session.commit()
            response.message(build_start_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "DONE":
            item = find_most_recent_due_item(user)
            if item is None:
                response.message("No due wash found right now. If you already replied, you're caught up.")
                return str(response), 200, {"Content-Type": "application/xml"}

            log_wash(item, source="done_reply")
            db.session.commit()

            primary_item = get_primary_item(user.id)
            avg_gap_days = calculate_average_gap_days(primary_item, user.timezone) if primary_item else 30.0
            tier_name = determine_tier(avg_gap_days)["name"]
            response.message(
                build_completion_message(
                    streak_display=format_streak_display(
                        calculate_streak(user.id),
                        primary_item.cadence_days if primary_item else 7,
                    ),
                    tier_name=tier_name,
                    next_label=format_date_label(item.next_reminder, user.timezone),
                )
            )
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "SKIP":
            item = find_most_recent_due_item(user)
            if item is None:
                response.message("No due wash found to skip right now.")
                return str(response), 200, {"Content-Type": "application/xml"}

            if item.skips_remaining <= 0:
                response.message(build_no_skips_message())
                return str(response), 200, {"Content-Type": "application/xml"}

            item.next_reminder = item.next_reminder + timedelta(days=1)
            item.skips_remaining -= 1
            clear_cycle_notification_state(item)
            db.session.commit()
            response.message(
                build_skip_message(
                    get_item_label(item.item_type),
                    format_date_label(item.next_reminder, user.timezone),
                    item.skips_remaining,
                )
            )
            return str(response), 200, {"Content-Type": "application/xml"}

        response.message(build_help_message())
        return str(response), 200, {"Content-Type": "application/xml"}

    should_start_scheduler = (
        not app.config.get("TESTING")
        and os.getenv("RUN_SCHEDULER", "1") == "1"
        and (os.getenv("WERKZEUG_RUN_MAIN") == "true" or not app.debug)
    )
    if should_start_scheduler:
        init_scheduler(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5003")),
        debug=True,
        use_reloader=False,
    )
