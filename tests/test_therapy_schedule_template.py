"""Contract checks for the therapy template and timed sending."""

from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flow_test.models import FlowJob
from flow_test.service import FlowTest, TestTransport
from services import therapy_schedule_template as template


def test_single_therapy_uses_one_red_line_and_singular_count():
    fields = template.template_fields([("Physio", time(10), time(10, 30))])
    assert fields == {
        "session_details": "🟥Physio: 10:00 am to 10:30 am",
        "therapy_count": "1 therapy",
        "message_context": ".",
    }


def test_multiple_therapies_use_a_single_line_and_plural_count():
    fields = template.template_fields([
        ("Physio", time(9), time(9, 40)),
        ("Speech\nTherapy", time(9, 40), time(10, 20)),
        ("Occupational Therapy", time(10, 35), time(11, 15)),
    ])
    assert fields["session_details"] == (
        "🟥Physio: 9:00 am to 9:40 am 🟥Speech Therapy: 9:40 am to 10:20 am "
        "🟥Occupational Therapy: 10:35 am to 11:15 am"
    )
    assert fields["therapy_count"] == "3 therapies"


def test_initial_and_reminder_use_numeric_parameter_order_and_the_same_yes_no_tokens(monkeypatch):
    monkeypatch.setenv("MAIN_WORKFLOW_MESSAGE_MODE", "template")
    monkeypatch.setenv("MAIN_WORKFLOW_TEMPLATE_PROFILE", template.PROFILE)
    monkeypatch.setenv("MAIN_WORKFLOW_TEMPLATE_NAME", "fictional_template")
    monkeypatch.setenv("MAIN_WORKFLOW_TEMPLATE_LANGUAGE", "en")
    monkeypatch.setenv("MAIN_WORKFLOW_TIMED_TEMPLATE_ENABLED", "false")
    transport = TestTransport(False, {"DUMMY": "919999999999"}, env_prefix="MAIN_WORKFLOW")
    transport.live, transport.sender = True, object()
    calls = []

    def fake_send(_settings, recipient, name, language, values, buttons):
        calls.append((recipient, name, language, values, buttons))
        return {"messages": [{"id": "fictional-message"}]}

    monkeypatch.setattr("flow_test.service.send_template_message", fake_send)
    fields = {"centre": "Fictional Centre", **template.template_fields([("Physio", time(10), time(10, 30))])}
    payload = {"recipient": "919999999999", "token": "same-token", "fields": fields,
               "button_prefix": "mw", "template_profile": template.PROFILE}
    transport.send("OFFER", payload)
    fields["message_context"] = template.REMINDER_CONTEXT
    payload["reminder"] = True
    transport.send("OFFER", payload)
    assert calls[0][3] == ("Fictional Centre", "🟥Physio: 10:00 am to 10:30 am", "1 therapy", ".")
    assert calls[1][3][3] == template.REMINDER_CONTEXT
    assert template.REMINDER_CONTEXT.startswith("*") and template.REMINDER_CONTEXT.endswith("*")
    assert calls[0][1:3] == calls[1][1:3] == ("fictional_template", "en")
    assert calls[0][4] == calls[1][4] == ("mw:same-token:YES", "mw:same-token:NO")


def test_disabled_workflow_does_not_even_open_a_database():
    flow = SimpleNamespace(live=True, transport=None)
    assert template.queue_daily_reminders(flow) == 0


def test_saved_initial_request_waits_until_nine_ist(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 8, 8, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(tz)

    monkeypatch.setattr(template, "datetime", Clock)
    flow = SimpleNamespace(transport=SimpleNamespace(mode="template", profile=template.PROFILE, timed_schedule=True))
    payload = {"template_profile": template.PROFILE, "template_timed": True,
               "fields": {"date": "2026-11-09"},
               "not_before": datetime(2026, 11, 8, 9, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()}
    job = SimpleNamespace(attempts=1)
    assert not template.prepare_template_job(flow, None, job, payload)
    assert job.status == "RETRY_WAIT"
    assert job.next_attempt_at == datetime(2026, 11, 8, 3, 30)
    assert job.attempts == 0


def test_successful_reminder_does_not_extend_the_original_reply_deadline():
    expiry = datetime(2026, 11, 8, 13, 30)
    offer = SimpleNamespace(state="PENDING", expires_at=expiry)
    job = SimpleNamespace()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, model, _key):
            return job if model is FlowJob else offer

    flow = SimpleNamespace(sessions=SimpleNamespace(begin=Session), live=False,
                           _pace_recipient=lambda *_: None)
    FlowTest._sent(flow, 1, "OFFER", {"token": "same-token", "reminder": True}, "fictional-message")
    assert offer.expires_at == expiry
