"""Scheduling assistant on the main workflow: opt-in, read-only, answers from the workflow DB.

Uses the fictional root test workflow and a fake Azure OpenAI; no network or real data.
"""
import io
import json
import threading

import pytest
from sqlalchemy import func, select

from database.models import PatientAbsence, TherapistAbsence
from tests.test_main_root_workflow import DAY, payload, root_main, root_store  # noqa: F401 - fixtures

TARGET = "https://example-ai.invalid/openai/responses?api-version=2025-04-01-preview"


@pytest.fixture
def assistant_on(monkeypatch):
    monkeypatch.setenv("SCHEDULING_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example-ai.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini")
    monkeypatch.setenv("AZURE_OPENAI_RESPONSES_URL", TARGET)


def fake_action(monkeypatch, action, entity=""):
    import web_app
    monkeypatch.setattr(web_app, "interpret_chat_action", lambda message, today=None: {
        "action": action, "patient_query": "", "entity_query": entity, "absence_kind": "unknown",
        "date": DAY.isoformat(), "reply": ""})


def test_assistant_stays_off_unless_enabled(root_main):
    assert payload("/api/assistant/status")[1] == {"configured": False}
    assert payload("/api/assistant/messages", {"message": "summary"})[0] == 404


def test_enabled_assistant_answers_from_the_workflow_database(root_main, assistant_on, monkeypatch):
    import web_app
    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: pytest.fail("Main storage was opened"))
    assert payload("/api/assistant/status")[1] == {"configured": True}
    fake_action(monkeypatch, "day_summary")
    status, body = payload("/api/assistant/messages", {"message": "Give me an overall summary for today"})
    assert status == 200 and body["outcome"] == "summary" and body["summary"]["scheduled"] >= 1


@pytest.mark.parametrize("action", ["mark_absent", "cancel_appointment"])
def test_assistant_is_read_only_on_the_main_workflow(root_main, assistant_on, monkeypatch, action):
    fake_action(monkeypatch, action, "TEST-A")
    with root_main.flow.sessions() as session:
        before = (session.scalar(select(func.count(PatientAbsence.id))), session.scalar(select(func.count(TherapistAbsence.id))))
    status, body = payload("/api/assistant/messages", {"message": "Mark TEST-A absent today"})
    assert status == 200 and body["outcome"] == "not_executed" and "questions only" in body["message"]
    with root_main.flow.sessions() as session:
        assert (session.scalar(select(func.count(PatientAbsence.id))), session.scalar(select(func.count(TherapistAbsence.id)))) == before


def test_assistant_does_not_wait_for_the_workflow_lock(root_main, assistant_on, monkeypatch):
    """A slow AI reply must never hold up offers or imports, so the assistant runs without that lock."""
    fake_action(monkeypatch, "needs_attention")
    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with root_main.flow.lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    held.wait(5)
    try:
        status, _ = payload("/api/assistant/messages", {"message": "What needs rescheduling?"})
        assert status == 200
    finally:
        release.set()
        holder.join()


def test_ai_call_uses_the_configured_thanal_deployment(assistant_on, monkeypatch):
    import web_app
    monkeypatch.setenv("AZURE_OPENAI_REASONING_EFFORT", "low")
    seen = {}

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, key=request.get_header("Api-key"), body=json.loads(request.data))
        action = {"action": "day_summary", "patient_query": "", "entity_query": "", "absence_kind": "unknown",
                  "date": DAY.isoformat(), "reply": "Here is the summary."}
        return Reply(json.dumps({"output_text": json.dumps(action)}).encode())

    monkeypatch.setattr(web_app, "urlopen", fake_urlopen)
    action = web_app.interpret_chat_action("summary for today", today=DAY)
    assert action["action"] == "day_summary"
    assert seen["url"] == TARGET and seen["key"] == "test-only-key"
    assert seen["body"]["model"] == "gpt-5.4-mini" and seen["body"]["reasoning"] == {"effort": "low"}


def test_a_missing_date_from_the_model_means_today(assistant_on, monkeypatch):
    import web_app

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    action = {"action": "needs_attention", "patient_query": "", "entity_query": "", "absence_kind": "unknown",
              "date": "", "reply": ""}
    monkeypatch.setattr(web_app, "urlopen", lambda request, timeout: Reply(json.dumps({"output_text": json.dumps(action)}).encode()))
    assert web_app.interpret_chat_action("What needs rescheduling?", today=DAY)["date"] == DAY.isoformat()
