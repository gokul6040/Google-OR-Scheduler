"""Consultant-only imports against disposable stores; no external calls."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from database.models import (Base, Department, Location, Patient, Therapist, TherapistCapability,
                             TherapistLocation, TreatmentRequirement)
from flow_test.models import Base as FlowBase, FlowSetting
from services.thanal_consultant_import import prepare_consultants, sync_consultants


def rows():
    return [{"ID": "100", "Name": "Fictional clinician", "Status": "Active",
             "Branch": {"ID": "10", "Branch_Name": "Fictional centre"},
             "Consultation_Type": "Full-Time", "Phone_Number": "unused",
             "Consultations": [{"ID": "20", "Value": "Speech"}, {"ID": "21", "Value": "Occupational"}]}]


@pytest.fixture
def flow():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    FlowBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as session:
        session.add(FlowSetting(key="main_workflow_source", value="thanal/eic"))
    yield SimpleNamespace(source_name="thanal/eic", is_test=False, sessions=sessions,
                          lock=nullcontext(), inbound_lock=nullcontext(), _audit=lambda *_: None)
    engine.dispose()


def test_one_consultant_many_therapies_and_idempotent_replay(flow):
    data = rows()
    original = deepcopy(data)
    assert sync_consultants(flow, data)["ready"]
    with flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Therapist)) == 0
    for _ in range(2):
        assert sync_consultants(flow, data, apply=True)["consultants_written"] == 1
    with flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Therapist)) == 1
        assert session.scalar(select(func.count()).select_from(TherapistCapability)) == 2
        assert session.scalar(select(func.count()).select_from(TherapistLocation)) == 1
        assert set(session.scalars(select(Department.name))) == {"Speech", "Occupational"}
        assert session.scalar(select(func.count()).select_from(Patient)) == 0
    assert data == original


def test_incomplete_master_blocks_atomic_apply(flow):
    data = rows()
    bad = deepcopy(data[0])
    bad.update(ID="101", Status="")
    data.append(bad)
    assert not sync_consultants(flow, data)["ready"]
    with pytest.raises(ValueError, match="no records"):
        sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Therapist)) == 0
        assert session.scalar(select(func.count()).select_from(Location)) == 0


def test_renamed_consultant_keeps_identity_and_staff_owned_status(flow):
    data = rows()
    sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        ident = session.scalar(select(Therapist.id))
    data[0].update(Name="Renamed fictional clinician", Status="Inactive")
    sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        doctor = session.get(Therapist, ident)
        # Zoho's status seeds a new consultant only; existing status belongs to staff.
        assert doctor.name == "Renamed fictional clinician" and doctor.active
    with flow.sessions.begin() as session:
        session.get(Therapist, ident).active = False  # Staff mark inactive in our app.
    data[0]["Status"] = "Active"
    sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        assert not session.get(Therapist, ident).active


def test_new_consultant_starts_with_zoho_status(flow):
    data = rows()
    data[0]["Status"] = "Inactive"
    sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        assert not session.scalar(select(Therapist.active))


def test_missing_rows_are_not_deletions_and_capacity_counts_people(flow):
    data = rows()
    second = deepcopy(data[0])
    second.update(ID="101", Name="Second fictional clinician")
    data.append(second)
    sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        assert session.scalar(select(Location.capacity)) == 2
    sync_consultants(flow, data[:1], apply=True)
    with flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Therapist)) == 2


@pytest.mark.parametrize("mutation", ["duplicate", "unknown_status", "employment_as_therapy", "missing_branch"])
def test_invalid_source_contract_is_not_guessed(mutation):
    data = rows()
    if mutation == "duplicate": data.append(deepcopy(data[0]))
    if mutation == "unknown_status": data[0]["Status"] = "Maybe"
    if mutation == "employment_as_therapy": data[0].pop("Consultations")
    if mutation == "missing_branch": data[0]["Branch"] = {}
    result = prepare_consultants(data)
    assert result["issues"]


def test_wrong_source_cannot_write(flow):
    flow.source_name = "other/app"
    with pytest.raises(ValueError, match="Thanal"):
        sync_consultants(flow, rows(), apply=True)


def test_capability_removal_with_an_assigned_plan_blocks_all_edits(flow):
    data = rows()
    sync_consultants(flow, data, apply=True)
    with flow.sessions.begin() as session:
        doctor = session.scalar(select(Therapist))
        patient = Patient(patient_code="FAKE", name="Fictional child")
        session.add(patient)
        session.flush()
        session.add(TreatmentRequirement(patient_id=patient.id, department_id=doctor.department_id,
            assigned_therapist_id=doctor.id, sessions_per_week=1, sequence_order=0, active=True))
    data[0]["Name"] = "Do not commit this edit"
    data[0]["Consultations"] = data[0]["Consultations"][:1]
    with pytest.raises(ValueError, match="no records"):
        sync_consultants(flow, data, apply=True)
    with flow.sessions() as session:
        assert session.scalar(select(Therapist.name)) == "Fictional clinician"
        assert session.scalar(select(func.count()).select_from(TherapistCapability)) == 2
