"""Deployment target, credential isolation and additive migration guarantees."""

from copy import deepcopy
import base64
import json
import zlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from main_workflow import thanal_release as release


@pytest.fixture
def app():
    identity = "/subscriptions/fictional/resourceGroups/thanal-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/test"
    values = {"MAIN_WORKFLOW_DATA_SOURCE": "main", "MAIN_WORKFLOW_ZOHO_OWNER": "thanal",
              "MAIN_WORKFLOW_ZOHO_APP": "eic", "MAIN_WORKFLOW_LIVE": "false"}
    env = [{"name": k, "value": v} for k,v in values.items()]
    env += [{"name": "THERAPY_SCHEDULER_DATABASE_URL", "secretRef": "db"},
            {"name": "WHATSAPP_ACCESS_TOKEN", "secretRef": "wa"},
            {"name": "MAIN_WORKFLOW_ZOHO_CLIENT_SECRET", "secretRef": "zoho"}]
    return {"location": "centralindia", "identity": {"userAssignedIdentities": {identity: {}}},
            "properties": {"managedEnvironmentId": "/fictional/environment", "configuration": {
                "activeRevisionsMode": "Single", "registries": [{"server": "thanalacr.azurecr.io", "identity": identity}]},
                "template": {"containers": [{"env": env}]}}}


def test_release_refuses_wrong_tenant_test_store_and_live_senders(app):
    account = {"id": release.SUB, "tenantId": release.TENANT}
    release.validate_target(account, app, app, [])
    with pytest.raises(release.ReleaseError, match="separate"):
        release.validate_target({"id": "turnb", "tenantId": "turnb"}, app, app, [])
    with pytest.raises(release.ReleaseError, match="worker"):
        release.validate_target(account, app, app, ["running-worker"])
    for key, value in [("MAIN_WORKFLOW_DATA_SOURCE", "test"), ("MAIN_WORKFLOW_LIVE", "true")]:
        wrong = deepcopy(app)
        next(r for r in release.app_env(wrong).values() if r["name"] == key)["value"] = value
        with pytest.raises(release.ReleaseError, match="setting"):
            release.validate_target(account, wrong, app, [])


def test_job_copies_no_whatsapp_credentials_and_roster_is_bounded_and_recoverable(app):
    secrets = {"db": "postgresql://fictional:NOT-REAL@thanalschedulerdb.postgres.database.azure.com/postgres", "wa": "NEVER-COPY-THIS", "zoho": "fake-zoho"}
    roster = [{"code": f"FAKE-{i}", "name": "Fictional child " + str(i), "approvedActive": i < 354} for i in range(512)]
    image = "thanalacr.azurecr.io/therapy-scheduler:fictional"
    job = release.job_document(app, secrets, image, "fake-job", "--inside-import", roster)
    assert "NEVER-COPY-THIS" not in json.dumps(job)
    env = {r["name"]: r for r in job["properties"]["template"]["containers"][0]["env"]}
    assert env["MAIN_WORKFLOW_LIVE"]["value"] == env["FLOW_TEST_LIVE"]["value"] == "false"
    assert "WHATSAPP_ACCESS_TOKEN" not in env
    chunks = {s["name"]: s["value"] for s in job["properties"]["configuration"]["secrets"]}
    n = int(env["THANAL_INITIAL_ROSTER_CHUNKS"]["value"])
    encoded = "".join(chunks[f"initial-roster-{i:03d}"] for i in range(n))
    assert all(len(chunks[f"initial-roster-{i:03d}"]) <= 2000 for i in range(n))
    assert json.loads(zlib.decompress(base64.b64decode(encoded))) == roster
    migration = release.job_document(app, secrets, image, "fake-db", "--inside-upgrade")
    assert "fake-zoho" not in json.dumps(migration)
    with pytest.raises(release.ReleaseError, match="registry"):
        release.job_document(app, secrets, "evil.invalid/image:latest", "fake-db", "--inside-upgrade")
    secrets["db"] = "postgresql://fake:SECRET@turnb.invalid/postgres"
    with pytest.raises(release.ReleaseError, match="verified PostgreSQL"):
        release.job_document(app, secrets, image, "fake-db", "--inside-upgrade")


def test_approved_roster_rejects_accidental_replacement(tmp_path):
    p = tmp_path / "fictional.json"
    p.write_text(json.dumps([{"code": "FAKE", "listed": True, "listed_name": "Fake child", "active": True,
                              "contact_issues": []}]), encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="512 unique"):
        release.approved_roster(p)


def test_inside_upgrade_preserves_clinical_rows_and_verifies_storage(monkeypatch):
    from database.db import Base
    from database.models import Patient
    from main_workflow.storage import prepare_storage, check_storage
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prepare_storage(engine, "thanal/eic")
    with Session(engine) as session:
        session.add(Patient(patient_code="FAKE-KEEP", name="Fictional patient", active=False))
        session.commit()
    monkeypatch.setenv("MAIN_WORKFLOW_LIVE", "false")
    monkeypatch.setattr("database.db.engine", engine)
    monkeypatch.setattr(release, "verify_database", lambda _: None)
    release.inside_upgrade()
    check_storage(engine)
    with Session(engine) as session:
        patient = session.scalar(select(Patient))
        assert patient.patient_code == "FAKE-KEEP" and not patient.active


def test_azure_cli_uses_utf8_for_windows_build_progress(monkeypatch, tmp_path):
    cli = tmp_path / "Microsoft SDKs/Azure/CLI2/python.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    azure = release.Azure()
    assert azure.env["PYTHONUTF8"] == "1" and azure.env["PYTHONIOENCODING"] == "utf-8"


def test_release_errors_show_location_without_secret_values(monkeypatch):
    def fail():
        raise ValueError("postgresql://fake:SECRET-MUST-NOT-APPEAR@db.invalid/postgres")
    monkeypatch.setattr(release, "inside_import", fail)
    monkeypatch.setattr("sys.argv", ["release", "--inside-import"])
    with pytest.raises(SystemExit) as error:
        release.main()
    message = str(error.value)
    assert "ValueError" in message and "in fail" in message
    assert "SECRET-MUST-NOT-APPEAR" not in message and "db.invalid" not in message
