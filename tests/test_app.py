import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest

os.environ["RUN_SCHEDULER"] = "0"
os.environ["EXPOSE_VERIFY_CODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from models import BeddingItem, User, WashLog, db, get_primary_item
from scheduler import (
    send_evening_followups,
    send_monthly_summary,
    send_morning_reminders,
    send_nextday_followups,
)


@pytest.fixture()
def app():
    db_fd, db_path = tempfile.mkstemp()
    test_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        }
    )
    with test_app.app_context():
        db.drop_all()
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()
    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


def setup_payload():
    return {
        "phone": "+13105551234",
        "timezone": "America/New_York",
        "last_washed": "2026-03-20",
        "unknown_last_washed": False,
        "sheets_cadence_days": 7,
        "add_ons": {
            "pillowcases": True,
            "duvet_cover": False,
            "mattress_protector": True,
        },
    }


def verify_user(client):
    setup_response = client.post("/api/setup", json=setup_payload())
    assert setup_response.status_code == 201
    setup_data = setup_response.get_json()

    verify_response = client.post(
        "/api/verify",
        json={
            "phone": setup_payload()["phone"],
            "code": setup_data["debug_verify_code"],
        },
    )
    assert verify_response.status_code == 200
    return verify_response.get_json()


def test_setup_verify_and_dashboard_flow(client):
    setup_response = client.post("/api/setup", json=setup_payload())
    assert setup_response.status_code == 201
    setup_data = setup_response.get_json()
    assert setup_data["verification_required"] is True
    assert setup_data["first_reminder"]["item_label"] in {
        "Sheets",
        "Pillowcases",
        "Mattress Protector",
    }
    assert setup_data["debug_verify_code"]

    verify_response = client.post(
        "/api/verify",
        json={
            "phone": setup_payload()["phone"],
            "code": setup_data["debug_verify_code"],
        },
    )
    assert verify_response.status_code == 200
    verify_data = verify_response.get_json()
    assert len(verify_data["dashboard"]["items"]) == 3
    assert verify_data["dashboard"]["tier"]["name"] in {
        "Fresh Prince",
        "Acceptable Adult",
        "Feral",
    }

    dashboard_response = client.get("/api/dashboard/%2B13105551234")
    assert dashboard_response.status_code == 200
    assert dashboard_response.get_json()["phone"] == "+13105551234"


def test_skip_limit_done_flow_and_monthly_summary(app, client):
    verify_user(client)

    with app.app_context():
        user = User.query.filter_by(phone="+13105551234").one()
        sheets = get_primary_item(user.id)
        assert sheets is not None
        for item in BeddingItem.query.filter_by(user_id=user.id, active=True).all():
            if item.id != sheets.id:
                item.next_reminder = date(2026, 4, 30)
                item.skips_remaining = 2
        sheets.next_reminder = date(2026, 3, 23)
        sheets.skips_remaining = 2
        db.session.commit()

    assert send_morning_reminders(app, datetime(2026, 3, 23, 13, 0))["sent"] >= 1

    first_skip = client.post("/sms/inbound", data={"From": "+13105551234", "Body": "SKIP"})
    assert first_skip.status_code == 200
    assert "1 skip left" in first_skip.get_data(as_text=True)

    with app.app_context():
        user = User.query.filter_by(phone="+13105551234").one()
        sheets = get_primary_item(user.id)
        assert sheets.next_reminder == date(2026, 3, 24)

    assert send_morning_reminders(app, datetime(2026, 3, 24, 13, 0))["sent"] >= 1
    second_skip = client.post("/sms/inbound", data={"From": "+13105551234", "Body": "SKIP"})
    assert second_skip.status_code == 200
    assert "No more skips left this cycle." in second_skip.get_data(as_text=True)

    assert send_morning_reminders(app, datetime(2026, 3, 25, 13, 0))["sent"] >= 1
    third_skip = client.post("/sms/inbound", data={"From": "+13105551234", "Body": "SKIP"})
    assert third_skip.status_code == 200
    assert "No more skips this cycle" in third_skip.get_data(as_text=True)

    assert send_evening_followups(app, datetime(2026, 3, 25, 22, 0))["sent"] >= 1
    assert send_nextday_followups(app, datetime(2026, 3, 26, 13, 0))["sent"] >= 1

    done_response = client.post("/sms/inbound", data={"From": "+13105551234", "Body": "DONE"})
    assert done_response.status_code == 200
    assert "tier unlocked" in done_response.get_data(as_text=True)

    with app.app_context():
        user = User.query.filter_by(phone="+13105551234").one()
        sheets = get_primary_item(user.id)
        assert sheets is not None
        assert sheets.skips_remaining == 2
        assert WashLog.query.filter_by(bedding_id=sheets.id).count() >= 1

    summary_result = send_monthly_summary(app, datetime(2026, 4, 1, 0, 0))
    assert summary_result["sent"] == 1
