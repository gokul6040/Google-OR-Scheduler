"""Staff "Refresh from Zoho": the nightly Thanal import run on demand. Fictional data only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import Patient
from main_workflow import refresh
from tests.test_thanal_daily_sync import flow, reports  # noqa: F401  (fixtures)

DAYTIME = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


def instance_for(flow, reports, fetch=None):
    return SimpleNamespace(flow=flow, config=flow.config, provider=SimpleNamespace(
        fetch=fetch or (lambda since, until: deepcopy(reports))))


def test_refresh_runs_the_nightly_import_and_reports_what_changed(flow, reports):
    instance = instance_for(flow, reports)
    started = refresh.start_refresh(instance, now=DAYTIME, run=False)
    assert started["state"] == "RUNNING"
    done = refresh.run_refresh(instance)
    assert done["state"] == "DONE"
    assert done["summary"]["newActive"] == ["FAKE-1"] and done["summary"]["messagesSent"] == 0
    assert refresh.read_status(flow)["state"] == "DONE"
    with flow.sessions() as session:
        assert session.scalar(select(Patient).where(Patient.patient_code == "FAKE-1")).active


def test_refresh_never_switches_an_inactive_child_on_but_shows_it_is_ready(flow, reports):
    with flow.sessions.begin() as session:
        session.add(Patient(patient_code="FAKE-1", name="Fictional child", active=False))
    instance = instance_for(flow, reports)
    refresh.start_refresh(instance, now=DAYTIME, run=False)
    done = refresh.run_refresh(instance)
    assert done["summary"]["readyToActivate"] == ["FAKE-1"] and done["summary"]["newActive"] == []
    with flow.sessions() as session:
        assert not session.scalar(select(Patient).where(Patient.patient_code == "FAKE-1")).active


def test_only_one_refresh_at_a_time_and_a_short_cool_down(flow, reports):
    instance = instance_for(flow, reports)
    refresh.start_refresh(instance, now=DAYTIME, run=False)
    with pytest.raises(ValueError, match="already running"):
        refresh.start_refresh(instance, now=DAYTIME + timedelta(minutes=1), run=False)
    refresh.run_refresh(instance)
    with pytest.raises(ValueError, match="wait"):
        refresh.start_refresh(instance, now=DAYTIME + timedelta(minutes=2), run=False)
    assert refresh.start_refresh(instance, now=DAYTIME + timedelta(minutes=6), run=False)["state"] == "RUNNING"


def test_a_stuck_run_stops_blocking_after_twenty_minutes(flow, reports):
    instance = instance_for(flow, reports)
    refresh.start_refresh(instance, now=DAYTIME, run=False)  # Never finishes (e.g. app restarted).
    assert refresh.start_refresh(instance, now=DAYTIME + timedelta(minutes=21), run=False)["state"] == "RUNNING"


@pytest.mark.parametrize("utc_hour,utc_minute", [(18, 20), (18, 45), (18, 59)])  # 23:50, 00:15, 00:29 IST
def test_no_refresh_around_the_midnight_import(flow, reports, utc_hour, utc_minute):
    with pytest.raises(ValueError, match="midnight"):
        refresh.start_refresh(instance_for(flow, reports),
                              now=datetime(2026, 11, 12, utc_hour, utc_minute, tzinfo=timezone.utc), run=False)


def test_a_failed_fetch_changes_nothing_and_is_reported_safely(flow, reports):
    def down(*_args):
        raise OSError("connection refused with secret-token-value")

    instance = instance_for(flow, reports, fetch=down)
    refresh.start_refresh(instance, now=DAYTIME, run=False)
    done = refresh.run_refresh(instance)
    assert done["state"] == "FAILED" and "secret" not in done["error"] and "Nothing was changed" in done["error"]
    with flow.sessions() as session:
        assert session.scalar(select(Patient)) is None


def test_refresh_is_only_for_the_thanal_source(flow, reports):
    instance = instance_for(flow, reports)
    instance.config = SimpleNamespace(source_name="other/app", plan_report="", registration_report="", consultant_report="")
    with pytest.raises(ValueError, match="Thanal"):
        refresh.start_refresh(instance, now=DAYTIME, run=False)


def test_route_starts_and_reports_a_refresh(flow, reports, monkeypatch):
    import json
    from main_workflow import mount

    instance = instance_for(flow, reports)
    instance.token = flow.config.admin_token
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    monkeypatch.setattr(mount, "enabled", lambda: True)
    monkeypatch.setattr(mount, "requires_access_key", lambda: True)
    monkeypatch.setattr(mount, "data_source", lambda: "main")
    monkeypatch.setenv("MAIN_WORKFLOW_ADMIN_TOKEN", flow.config.admin_token)
    monkeypatch.setattr(refresh, "near_nightly_import", lambda now: False)  # Run whatever the real clock says.
    started = []
    monkeypatch.setattr(refresh.threading, "Thread", lambda target, args, **_: SimpleNamespace(
        start=lambda: started.append(target(*args))))
    sent = []

    def request(method, body=None):
        return SimpleNamespace(command=method, path="/api/zoho/refresh",
                               headers={"Authorization": f"Bearer {flow.config.admin_token}"},
                               read_json=lambda: body or {}, close_connection=False,
                               send_json=lambda payload, status=200: sent.append((status, payload)))

    mount.dispatch(request("POST", {}), prefix="")
    assert sent[-1][0] == 202 and started and started[0]["state"] == "DONE"
    mount.dispatch(request("GET"), prefix="")
    assert sent[-1][0] == 200 and sent[-1][1]["state"] == "DONE"
    mount.dispatch(request("POST", {}), prefix="")  # Within the cool-down.
    assert sent[-1][0] == 409 and "wait" in sent[-1][1]["error"]
    assert json.dumps(sent)  # Responses are plain JSON.
