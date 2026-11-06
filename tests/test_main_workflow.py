"""Opt-in main operations use existing records, never test storage or test seeds."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from database.db import Base
from database.models import Appointment, AuditLog, Department, Location, Patient, Therapist, TreatmentRequirement
from flow_test.models import FlowJob, FlowOffer
from flow_test.zoho_import import apply_reports
from main_workflow import mount
from main_workflow.config import WorkflowConfig
from main_workflow.provider import ZohoProvider
from main_workflow.service import MainWorkflow
from main_workflow.storage import prepare_storage
from tests.test_flow_test_mount import request


DAY = date(2026, 10, 23)
KEY = "dummy-main-admin-key-at-least-32-characters"


@pytest.fixture
def main_operations(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'normal.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine).begin() as session:
        session.add(Patient(patient_code="EXISTING", name="Existing local record", active=True))
    config = WorkflowConfig(KEY, "clinic-operator", "fictional-clinic", "eic-pilot", "Plan_Report",
                            "All_Consultants", "Scheduler", "Scheduler_Report")
    before = set(inspect(engine).get_table_names())
    migration = prepare_storage(engine, config.source_name)
    assert len(migration["tablesAdded"]) == 5
    assert set(inspect(engine).get_table_names()) == before | set(migration["tablesAdded"])
    calls = []
    flow = MainWorkflow(engine, config, send=lambda kind, payload: calls.append((kind, deepcopy(payload))) or f"dummy-{len(calls)}")
    flow.next_offer_date = lambda: DAY
    instance = SimpleNamespace(flow=flow, config=config, token=KEY, provider=ZohoProvider(config), previews={},
                               zoho_dispatch=mount.MainRuntime.zoho_dispatch, calendar_push=mount.MainRuntime.calendar_push)
    monkeypatch.setenv("MAIN_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("FLOW_TEST_LIVE", "false")
    monkeypatch.setenv("MAIN_WORKFLOW_ADMIN_TOKEN", KEY)
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    yield instance, reports, calls
    engine.dispose()


def import_source(instance, reports):
    return request("/workflow/api/zoho/import", {"source": instance.config.source_name,
                   "sourceTimezones": {"Added_Time": "Asia/Kolkata", "Modified_Time": "Asia/Kolkata"},
                   "syncWindow": {"from": None, "to": "2026-10-24T00:00:00Z"}, "reports": reports}, token=KEY)


def test_main_workflow_is_off_by_default_and_does_not_open_a_database(monkeypatch):
    monkeypatch.delenv("MAIN_WORKFLOW_ENABLED", raising=False)
    monkeypatch.setattr(mount, "runtime", lambda: pytest.fail("Disabled workflow opened a runtime"))
    assert json.loads(request("/api/workflow/status")[1]) == {"enabled": False, "path": None}
    assert request("/workflow")[0] == 404
    assert request("/workflow/api/zoho/import", {"reports": {}})[0] == 404


def test_explicit_migration_is_idempotent_and_main_startup_never_seeds(main_operations):
    instance, _reports, calls = main_operations
    assert prepare_storage(instance.flow.engine, instance.config.source_name)["tablesAdded"] == []
    with instance.flow.sessions() as session:
        assert list(session.scalars(select(Patient.patient_code))) == ["EXISTING"]
        assert not list(session.scalars(select(Therapist)))
        assert not list(session.scalars(select(Appointment)))
    assert calls == []


def test_main_import_keeps_a_returning_patients_courses_separate(main_operations):
    from database.models import AppointmentStatus
    from services.parent_response_service import weekly_progress

    instance, reports, calls = main_operations
    assert import_source(instance, reports)[0] == 200
    with instance.flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-B"))
        old = session.scalar(select(TreatmentRequirement).where(
            TreatmentRequirement.patient_id == patient.id, TreatmentRequirement.active.is_(True)))
        old_id, patient_id, old_start = old.id, patient.id, old.plan_start_date
        session.add(Appointment(patient_id=patient.id, treatment_requirement_id=old.id,
                                therapist_id=old.assigned_therapist_id, department_id=old.department_id,
                                location_id=old.location_id, date=DAY, slot_index=0,
                                status=AppointmentStatus.COMPLETED))
    new = deepcopy(next(r for r in reports["Plan_Report"] if r["RegistrationID"] == "TEST-B"))
    new.update(ID="900003", Added_Time="24-Oct-2026 11:30:00", Modified_Time="24-Oct-2026 11:30:00")
    instance.flow.next_offer_date = lambda: date(2026, 10, 24)
    status, raw = request("/workflow/api/zoho/import", {
        "source": instance.config.source_name, "reports": {"Plan_Report": [new], "All_Consultants": []},
        "sourceTimezones": {"Added_Time": "Asia/Kolkata", "Modified_Time": "Asia/Kolkata"},
        "syncWindow": {"from": "2026-10-24T00:00:00Z", "to": "2026-10-25T00:00:00Z"},
    }, token=KEY)
    assert status == 200, raw
    assert json.loads(raw)["messages_sent"] == 0
    with instance.flow.sessions() as session:
        old = session.get(TreatmentRequirement, old_id)
        assert not old.active and old.plan_start_date == old_start
        current = session.scalar(select(TreatmentRequirement).where(
            TreatmentRequirement.patient_id == patient_id, TreatmentRequirement.active.is_(True)))
        assert current.source_plan_id == "900003" and current.id != old_id
        progress = next(p for p in weekly_progress(session, DAY) if p.requirement_id == current.id)
        assert progress.completed == 0 and progress.remaining == current.sessions_per_week
        assert session.scalar(select(Appointment).where(
            Appointment.treatment_requirement_id == old_id)).status == AppointmentStatus.COMPLETED
    assert calls == []


def test_main_auth_and_reset_simulation_boundaries(main_operations):
    instance, _reports, calls = main_operations
    assert request("/workflow/api/state", token="wrong")[0] == 401
    for path in ("reset", "simulate", "generate"):
        assert request("/workflow/api/" + path, {}, token=KEY)[0] == 404
    assert request("/workflow/.env", token=KEY)[0] == 404
    with pytest.raises(ValueError, match="reset"):
        instance.flow.reset_test_data("RESET flow_test")
    assert calls == []


def test_main_import_review_save_reply_attendance_and_export_reuse(main_operations):
    from flow_test.service import format_offer_message
    from flow_test.zoho_export import prepare_export

    instance, reports, calls = main_operations
    status, raw = import_source(instance, reports)
    assert status == 200, raw
    assert json.loads(raw)["patients"] == 8
    assert "EXISTING" in {patient["code"] for patient in instance.flow.scenario["patients"]}
    with instance.flow.sessions() as session:
        assert session.scalar(select(Patient).where(Patient.patient_code == "EXISTING")).active
        assert not list(session.scalars(select(Appointment)))
    assert calls == []
    code, raw = request("/workflow/api/schedules/generate", {"start": DAY.isoformat(), "patients": ["TEST-H"]}, token=KEY)
    assert code == 201, raw
    preview = json.loads(raw)
    code, raw = request(f"/workflow/api/schedules/{preview['previewId']}/save", {}, token=KEY)
    assert code == 200, raw
    assert json.loads(raw)["offers"] == 1
    instance.flow.drain()
    assert len(calls) == 1
    assert calls[0][1]["button_prefix"] == "mw"
    assert "TEST ONLY" not in format_offer_message(calls[0][1]["fields"])
    assert "FICTIONAL TEST" not in calls[0][1]["fields"]["centre"]
    with instance.flow.sessions.begin() as session:
        offer = session.scalar(select(FlowOffer))
        instance.flow._job(session, "main-yes", "INBOUND", {"from": offer.recipient,
                           "timestamp": str(int(time.time())), "button": {"payload": f"mw:{offer.token}:YES"}})
    instance.flow.drain()
    appointment = instance.flow.state(DAY)["appointments"][0]
    assert appointment["status"] == "CONFIRMED"
    assert "TEST ONLY" not in calls[-1][1]["text"]
    assert request("/workflow/api/attendance", {"id": appointment["id"], "status": "COMPLETED"}, token=KEY)[0] == 200
    batch = prepare_export(instance.flow, DAY)
    assert batch["target"] == instance.config.export_target
    assert batch["changes"][0]["fields"]["Scheduler_Key"].startswith("main-")
    assert batch["changes"][0]["fields"]["Status"] == "COMPLETED"
    with instance.flow.sessions() as session:
        assert {audit.user for audit in session.scalars(select(AuditLog).where(AuditLog.action.like("flow_%")))} == {"clinic-operator"}


def test_main_rejects_an_import_for_the_test_source(main_operations):
    instance, reports, calls = main_operations
    code, _ = request("/workflow/api/zoho/import", {"source": "nidhin.kumar_turnb/test-gokul", "reports": reports}, token=KEY)
    assert code == 409
    with instance.flow.sessions() as session:
        assert list(session.scalars(select(Patient.patient_code))) == ["EXISTING"]
    assert calls == []


def test_main_requires_explicit_configuration_and_never_uses_test_credentials(monkeypatch):
    for key in ("ADMIN_TOKEN", "ADMIN_ID", "ZOHO_OWNER", "ZOHO_APP", "ZOHO_PLAN_REPORT",
                "ZOHO_CONSULTANT_REPORT", "ZOHO_SCHEDULE_FORM", "ZOHO_SCHEDULE_REPORT", "SOURCE_TIMEZONE"):
        monkeypatch.delenv("MAIN_WORKFLOW_" + key, raising=False)
    monkeypatch.setenv("FLOW_TEST_ADMIN_TOKEN", KEY)
    with pytest.raises(ValueError, match="separate main-workflow"):
        WorkflowConfig.from_environment()


def test_main_daily_planning_uses_normal_solver_settings(main_operations, monkeypatch):
    from config import SOLVER_SETTINGS

    instance, reports, _calls = main_operations
    assert import_source(instance, reports)[0] == 200
    seen = []
    original = instance.flow._daily_result

    def capture(session, target, patients, settings):
        seen.append(settings)
        return original(session, target, patients, settings)

    monkeypatch.setattr(instance.flow, "_daily_result", capture)
    result, _pinned = instance.flow.preview_daily(DAY, ["TEST-H"])
    assert seen == [SOLVER_SETTINGS]
    assert result.status in {"FEASIBLE", "OPTIMAL"} and not any(result.validation.values())


def test_main_shell_reuses_the_ui_but_has_its_own_login(main_operations):
    code, html = request("/workflow")
    assert code == 200 and b"/workflow/workspace.js" in html
    assert b"/test/workspace.js" not in html
    assert b"mainWorkflowAdminToken" in request("/workflow/workspace.js")[1]


def test_main_import_blocks_duplicate_existing_consultant_identity_atomically(main_operations):
    instance, reports, _calls = main_operations
    with instance.flow.sessions.begin() as session:
        department = Department(name="Psychology Therapy Session")
        location = Location(name="Malaparamba EIC", capacity=1)
        session.add_all([department, location])
        session.flush()
        session.add(Therapist(therapist_code="LEGACY-DOCTOR", name="TEST CONSULTANT P",
                              department_id=department.id, location_id=location.id, active=True))
    code, raw = import_source(instance, reports)
    assert code == 409 and b"identity mapping" in raw
    with instance.flow.sessions() as session:
        assert list(session.scalars(select(Therapist.therapist_code))) == ["LEGACY-DOCTOR"]
        assert list(session.scalars(select(Patient.patient_code))) == ["EXISTING"]


def test_main_missing_contact_cannot_save_even_in_preview(main_operations):
    instance, reports, calls = main_operations
    assert import_source(instance, reports)[0] == 200
    with instance.flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).where(Patient.patient_code == "EXISTING"))
        doctor = session.scalar(select(Therapist))
        session.add(TreatmentRequirement(patient_id=patient.id, department_id=doctor.department_id,
                    location_id=doctor.location_id, assigned_therapist_id=doctor.id, sessions_per_week=1,
                    sequence_order=0, active=True))
    code, raw = request("/workflow/api/schedules/generate", {"start": DAY.isoformat(), "patients": ["EXISTING"]}, token=KEY)
    assert code == 201, raw
    preview = json.loads(raw)
    assert preview["appointments"]
    code, raw = request(f"/workflow/api/schedules/{preview['previewId']}/save", {}, token=KEY)
    assert code == 409 and b"verified parent contacts" in raw
    assert not instance.flow.state(DAY)["appointments"]
    assert calls == []


def test_main_webhook_uses_its_own_button_namespace(main_operations, monkeypatch):
    instance, reports, _calls = main_operations
    assert import_source(instance, reports)[0] == 200
    flow = instance.flow
    flow.generate(DAY, ["TEST-H"])
    flow.publish(DAY)
    flow.drain()
    offer = flow.state(DAY)["offers"][0]
    flow.live = True  # Routing only: the injected sender never calls Meta.
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "dummy-business")

    def event(prefix):
        return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "dummy-business"},
            "messages": [{"id": f"{prefix}-callback", "from": flow.recipients["TEST-H"],
                          "timestamp": str(int(time.time())),
                          "button": {"payload": f"{prefix}:{offer['token']}:YES"}}]}}]}]}

    rest = mount.route_webhook(event("ft"))
    assert len(rest["entry"][0]["changes"][0]["value"]["messages"]) == 1
    assert flow.state(DAY)["appointments"][0]["status"] == "PENDING_CONFIRMATION"
    rest = mount.route_webhook(event("mw"))
    assert not rest["entry"][0]["changes"][0]["value"]["messages"]
    flow.process_one()
    assert flow.state(DAY)["appointments"][0]["status"] == "CONFIRMED"


def test_main_oauth_token_does_not_replace_legacy_or_test_tokens(tmp_path, monkeypatch):
    from flow_test.zoho_authorize import save_test_refresh_token

    path = tmp_path / "main.env"
    path.write_text("ZOHO_CREATOR_REFRESH_TOKEN=legacy-dummy\nFLOW_TEST_ZOHO_REFRESH_TOKEN=test-dummy\n")
    monkeypatch.setenv("MAIN_WORKFLOW_ZOHO_REFRESH_TOKEN", "old-main-dummy")
    save_test_refresh_token(path, "new-main-dummy", key="MAIN_WORKFLOW_ZOHO_REFRESH_TOKEN")
    assert path.read_text() == ("ZOHO_CREATOR_REFRESH_TOKEN=legacy-dummy\nFLOW_TEST_ZOHO_REFRESH_TOKEN=test-dummy\n"
                                "MAIN_WORKFLOW_ZOHO_REFRESH_TOKEN=new-main-dummy\n")
