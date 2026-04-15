from __future__ import annotations

import logging
import os

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from models import get_item_label


logger = logging.getLogger(__name__)


HYGIENE_FACTS = [
    "Your sheets collect about 1.5 grams of dead skin every week.",
    "Dust mites thrive in unwashed bedding, especially in warm rooms.",
    "Clean sheets can help sleep feel deeper and less itchy.",
    "Hot water is better at knocking out dust mites and bacteria.",
    "The average person sweats a surprising amount in bed over a year.",
    "Pillowcases deserve more washes than sheets because they touch your face.",
    "Fresh bedding can help cut down on stale odors in the whole room.",
    "Skin cells, oils, and sweat build up even when the bed looks fine.",
    "Dust mites love humidity, which makes bedding a great little resort.",
    "Washing on schedule is easier than waiting until the bed feels gross.",
    "Your bed keeps collecting residue while you keep pretending it is fine.",
    "Clean sheets are one of the cheapest quality-of-life upgrades at home.",
    "Pillowcases can quietly collect hair product, makeup, and skin oil fast.",
    "The fresher the bedding, the easier it is to fall into clean-sleep mode.",
    "Cold water saves energy, but it is worse at dealing with dust mites.",
    "Duvet covers trap more body oil than people think.",
    "Mattress protectors need washes too, not just good intentions.",
    "A weekly sheet habit beats a panic wash before guests arrive.",
    "Fresh bedding can help sensitive skin calm down faster.",
    "You spend a third of your life in bed, which is rude if the sheets are stale.",
    "Hotel sheets get washed after every guest. Your bed has much lower standards.",
    "Dust, skin, and sweat never take a week off.",
]


DAY_OF_REMINDERS = [
    "Sheet day! Your future self will thank you tonight. {fact} Reply DONE when done.",
    "Time to strip the bed. Two minutes of effort, a week of clean sleep. {fact} Reply DONE when done.",
    "Dead skin has been collecting on your {item_label} for {days} days. Today's the day. {fact} Reply DONE when done.",
    "The bed bugs are probably imaginary. The dust mites are not. Wash day. {fact} Reply DONE when done.",
    "Fresh sheet night is the closest thing to a spa you can get for free. {fact} Reply DONE when done.",
    "Laundry reminder from the version of you who likes clean sleep: wash the {item_label}. {fact} Reply DONE when done.",
    "Your {item_label} are due, and tonight could still be elite. {fact} Reply DONE when done.",
    "No chaos required. Just wash the {item_label} and collect your reward tonight. {fact} Reply DONE when done.",
    "Morning assignment: make the bed gross for one minute so it can be clean for seven nights. {fact} Reply DONE when done.",
    "The washer misses you. More specifically, it misses your {item_label}. {fact} Reply DONE when done.",
    "A clean-bed day starts with stripping the {item_label}. {fact} Reply DONE when done.",
    "You are one load away from vastly better sleep propaganda. Wash the {item_label}. {fact} Reply DONE when done.",
    "This is your polite notice that the {item_label} have reached their limit. {fact} Reply DONE when done.",
]


EVENING_FOLLOWUPS = [
    "Still haven't washed those {item_label}? No judgment. Okay, a little judgment. {fact} Reply DONE when done.",
    "The {item_label} are not going to wash themselves. We checked. {fact} Reply DONE when done.",
    "Your bed is currently a dust mite resort. Checkout time. {fact} Reply DONE when done.",
    "It is still sheet day, technically. There is time to save this. {fact} Reply DONE when done.",
    "Evening check-in: the washer is still available and the {item_label} are still due. {fact} Reply DONE when done.",
    "A clean-bed tonight is still on the table if you move now. {fact} Reply DONE when done.",
]


NEXT_DAY_FOLLOWUPS = [
    "Day 2. The dust mites are throwing a party on your {item_label}. {fact} Reply DONE when done.",
    "Still? We believe in you. The washing machine remains extremely close by. {fact} Reply DONE when done.",
    "Two days past due. This is your gentle but firm nudge. {fact} Reply DONE when done.",
    "Clean-sheet night keeps trying to happen for you. Meet it halfway. {fact} Reply DONE when done.",
    "Your overdue {item_label} would like to stop being a science project. {fact} Reply DONE when done.",
    "This is the follow-up after the follow-up. The message is still wash the {item_label}. {fact} Reply DONE when done.",
]


def build_verification_message(code: str) -> str:
    return f"Fresh Sheets verification code: {code}. Reply STOP any time to pause reminders."


def build_help_message() -> str:
    return "Fresh Sheets commands: DONE logs a wash, SKIP pushes by 1 day, STOP pauses, START resumes."


def build_stop_message() -> str:
    return "Fresh Sheets paused. No more reminders until you reply START."


def build_start_message() -> str:
    return "Fresh Sheets is back on. We will text you the morning your next item is due."


def build_welcome_message(next_sheet_day_label: str) -> str:
    return f"Fresh Sheets is live. Your next sheet day: {next_sheet_day_label}. Reply DONE when you wash."


def build_completion_message(
    *,
    streak_display: str,
    tier_name: str,
    next_label: str,
) -> str:
    streak_line = f" {streak_display}." if streak_display else ""
    return f"Logged. {tier_name} tier unlocked.{streak_line} Next reminder: {next_label}."


def build_skip_message(item_label: str, next_label: str, skips_remaining: int) -> str:
    if skips_remaining == 1:
        remainder = "1 skip left this cycle."
    elif skips_remaining <= 0:
        remainder = "No more skips left this cycle."
    else:
        remainder = f"{skips_remaining} skips left this cycle."
    return f"{item_label} pushed to {next_label}. {remainder}"


def build_no_skips_message() -> str:
    return "No more skips this cycle. Time to face the sheets."


def build_monthly_summary(
    *,
    month_name: str,
    washes: int,
    average_gap_days: float,
    tier_name: str,
) -> str:
    return (
        f"{month_name}: You washed sheets {washes} times. "
        f"Average gap: {average_gap_days:.1f} days. Tier: {tier_name}. Keep it up."
    )


def choose_message(seed: int, templates: list[str]) -> str:
    return templates[seed % len(templates)]


def choose_fact(seed: int) -> str:
    return HYGIENE_FACTS[seed % len(HYGIENE_FACTS)]


def build_day_of_message(*, item_type: str, days_since: int, seed: int) -> str:
    return choose_message(seed, DAY_OF_REMINDERS).format(
        item_label=get_item_label(item_type).lower(),
        days=days_since,
        fact=choose_fact(seed * 3 + days_since),
    )


def build_evening_message(*, item_type: str, seed: int) -> str:
    return choose_message(seed, EVENING_FOLLOWUPS).format(
        item_label=get_item_label(item_type).lower(),
        fact=choose_fact(seed * 5 + len(item_type)),
    )


def build_nextday_message(*, item_type: str, seed: int) -> str:
    return choose_message(seed, NEXT_DAY_FOLLOWUPS).format(
        item_label=get_item_label(item_type).lower(),
        fact=choose_fact(seed * 7 + len(item_type)),
    )


def send_sms_message(to_number: str, body: str) -> str:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")

    if not all([account_sid, auth_token, from_number]):
        logger.info("Twilio credentials missing. Mocking send to %s: %s", to_number, body)
        return "mock-message-sid"

    client = Client(account_sid, auth_token)
    try:
        message = client.messages.create(body=body, from_=from_number, to=to_number)
        return message.sid
    except TwilioRestException as exc:  # pragma: no cover - third-party failure
        logger.exception("Twilio send failed: %s", exc)
        raise
