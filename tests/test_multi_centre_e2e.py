"""End to end across several centres with their own shifts (19 Nov 2026).

Runs the real night plan (plan, check, save, queue offers), the WhatsApp offer text,
parent replies, replacement suggestions and the Zoho row builder on four fictional
centres. Nothing is sent: the transport is non-live and ``send`` is a local recorder.
"""

from collections import defaultdict
from datetime import date, datetime
import json
import re

import pytest
from sqlalchemy import select

from database.models import Appointment, AppointmentStatus, Location, Patient, Therapist
from flow_test.models import FlowInbox, FlowJob, FlowOffer
from flow_test.service import FlowTest, format_offer_message
from main_workflow import daily_plan, thanal_export
from scheduling.slot_config import slot_def
from services.shift_service import archive_shift, assign_shift, load_shift_context, save_shift

MONDAY = date(2026, 10, 19)
LENGTH = {"North": 40, "South": 50, "East": 30}


def rows(*spec):
    return [{"kind": "break", "breakKind": kind[6:], "start": start, "end": end} if kind.startswith("break")
            else {"kind": "session", "start": start, "end": end} for kind, start, end in spec]


LATE_40 = rows(("session", "09:40", "10:20"), ("break:short", "10:20", "10:35"), ("session", "10:35", "11:15"),
               ("session", "11:15", "11:55"), ("session", "11:55", "12:35"), ("break:lunch", "12:35", "13:15"),
               ("session", "13:15", "13:55"), ("session", "13:55", "14:35"))
FIFTY = rows(("session", "09:00", "09:50"), ("session", "09:50", "10:40"), ("break:short", "10:40", "10:55"),
             ("session", "10:55", "11:45"), ("session", "11:45", "12:35"), ("break:lunch", "12:35", "13:15"),
             ("session", "13:15", "14:05"), ("session", "14:05", "14:55"))
THIRTY = rows(("session", "09:00", "09:30"), ("session", "09:30", "10:00"), ("session", "10:00", "10:30"),
              ("break:short", "10:30", "10:45"), ("session", "10:45", "11:15"), ("session", "11:15", "11:45"),
              ("session", "11:45", "12:15"))


def child(code, *plan):
    return {"code": code, "name": f"Fictional Child {code}", "initial_roster": True,
            "requirements": [{"therapy": therapy, "therapist": doctor, "weekly": weekly}
                             for therapy, doctor, weekly in plan]}


SCENARIO = {
    "version": "multi-centre-e2e-v1", "centre": "North EIC — FICTIONAL TEST", "capacity": 3,
    "locations": [{"name": f"{name} EIC — FICTIONAL TEST", "capacity": 3, "branch": name}
                  for name in ("North", "South", "East", "West")],
    "therapists": [{"code": f"ZC-9{index:04d}", "name": f"{centre} {therapy} doctor", "therapy": therapy,
                    "branch": centre}
                   for index, (centre, therapy) in enumerate([
                       ("North", "Speech"), ("North", "OT"), ("South", "Speech"), ("South", "OT"),
                       ("East", "Speech"), ("West", "Speech")])],
    "patients": [
        child("N-A", ("Speech", "ZC-90000", 3), ("OT", "ZC-90001", 2)),
        child("N-B", ("Speech", "ZC-90000", 2)),
        child("N-C", ("OT", "ZC-90001", 2)),
        child("S-A", ("Speech", "ZC-90002", 3), ("OT", "ZC-90003", 2)),
        child("S-B", ("OT", "ZC-90003", 2)),
        child("E-A", ("Speech", "ZC-90004", 3)),
        child("E-B", ("Speech", "ZC-90004", 2)),
        child("E-C", ("Speech", "ZC-90004", 2)),
        child("W-A", ("Speech", "ZC-90005", 2)),
    ],
}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    # Thanal's production message mode: timed four-variable template, held until 09:00 IST.
    monkeypatch.setenv("FLOW_TEST_MESSAGE_MODE", "template")
    monkeypatch.setenv("FLOW_TEST_TEMPLATE_PROFILE", "therapy_schedule_v1")
    monkeypatch.setenv("FLOW_TEST_TIMED_TEMPLATE_ENABLED", "true")
    flow = FlowTest(f"sqlite:///{(tmp_path / 'centres.sqlite').as_posix()}", scenario=SCENARIO)
    assert flow.transport is not None and not flow.transport.live
    flow.sent = []
    flow.send = lambda kind, payload: flow.sent.append((kind, payload)) or f"preview-{len(flow.sent)}"
    flow.next_offer_date = lambda: MONDAY
    with flow.sessions.begin() as session:
        centre = {location.name.split()[0]: location.id for location in session.scalars(select(Location))}
        doctor = {item.therapist_code: item.id for item in session.scalars(select(Therapist))}
        # North: Shift 1 plus a later 40-minute shift; its Speech doctor works the later one.
        late = save_shift(session, location_id=centre["North"], name="Late", items=LATE_40, actor="t", activate=True)
        assign_shift(session, [doctor["ZC-90000"]], late["shift"]["id"], "t")
        # South and East: Shift 1 off, then a 50- and a 30-minute shift. West: Shift 1 off, nothing new.
        for name, items in (("South", FIFTY), ("East", THIRTY), ("West", None)):
            archive_shift(session, None, "t", location_id=centre[name], acknowledge_affected=True)
            if items:
                save_shift(session, location_id=centre[name], name=f"{name} day", items=items, actor="t",
                           activate=True, acknowledge_affected=True)
    flow.centre, flow.doctor = centre, doctor
    yield flow
    flow.engine.dispose()


def minutes(slot):
    window = slot_def(slot)
    return (window.end_time.hour * 60 + window.end_time.minute) - (window.start_time.hour * 60 + window.start_time.minute)


def saved_visits(flow):
    with flow.sessions() as session:
        return list(session.scalars(select(Appointment).where(Appointment.date == MONDAY)))


def test_night_plan_follows_every_centres_own_shifts_and_queues_offers_without_sending(flow):
    result = daily_plan.plan_and_save(flow, MONDAY)
    assert result["status"] == "SAVED" and result["offers"] > 0
    visits = saved_visits(flow)
    by_centre = {name: [item for item in visits if item.location_id == location_id]
                 for name, location_id in flow.centre.items()}
    assert all(by_centre[name] for name in ("North", "South", "East"))
    assert by_centre["West"] == []  # No active shift: its clinician is not scheduled.

    with flow.sessions() as session:
        context = load_shift_context(session)
        doctors = {item.id: item for item in session.scalars(select(Therapist))}
        for item in visits:
            # Every visit is inside its own doctor's shift and uses its centre's session length.
            assert item.slot_index in context.therapist_sessions(item.therapist_id, doctors[item.therapist_id].location_id)
            centre = next(name for name, location_id in flow.centre.items() if location_id == item.location_id)
            assert minutes(item.slot_index) == LENGTH[centre]
        late_ids = set(context.sessions(next(shift.id for shift in context.shifts.values() if shift.name == "Late")))
    north_speech = [item for item in visits if item.therapist_id == flow.doctor["ZC-90000"]]
    assert north_speech and all(item.slot_index in late_ids for item in north_speech)

    # No doctor, child or room is double-booked by clock time, even across different session lengths.
    for key in (lambda item: ("doctor", item.therapist_id), lambda item: ("child", item.patient_id)):
        grouped = defaultdict(list)
        for item in visits:
            grouped[key(item)].append(slot_def(item.slot_index))
        for windows in grouped.values():
            windows.sort(key=lambda window: window.start_time)
            assert all(first.end_time <= second.start_time for first, second in zip(windows, windows[1:]))
    with flow.sessions() as session:
        assert not session.scalars(select(FlowInbox).where(FlowInbox.reason.like("%booking review%"))).first()
        jobs = list(session.scalars(select(FlowJob).where(FlowJob.kind == "OFFER")))
    assert len(jobs) == result["offers"]

    # WhatsApp offers are created (template held until 09:00 IST the day before) and never sent here.
    flow.drain()
    assert flow.sent == []
    clock = re.compile(r"(\d{1,2}):(\d{2}) ([ap]m) to (\d{1,2}):(\d{2}) ([ap]m)")
    for job in jobs:
        payload = json.loads(job.payload)
        fields = payload["fields"]
        assert payload["template_profile"] == "therapy_schedule_v1" and payload["template_timed"] is True
        assert datetime.fromtimestamp(payload["not_before"]).date() <= MONDAY
        centre = fields["centre"].split()[0]
        times = clock.findall(fields["session_details"])
        assert times and fields["therapy_count"].startswith(str(len(times)))
        for h1, m1, p1, h2, m2, p2 in times:
            start = int(h1) % 12 * 60 + int(m1) + (720 if p1 == "pm" else 0)
            end = int(h2) % 12 * 60 + int(m2) + (720 if p2 == "pm" else 0)
            assert end - start == LENGTH[centre]  # The message shows the centre's real session times.
        text = format_offer_message(fields)
        assert fields["centre"] in text and "Can you attend?" in text and len(text) <= 1024


def test_replies_and_replacements_stay_inside_each_centre_and_shift(flow):
    daily_plan.plan_and_save(flow, MONDAY)
    with flow.sessions() as session:
        offers = {offer.patient_code: offer.token for offer in session.scalars(select(FlowOffer))}
    yes = next(code for code in offers if code.startswith("S-"))
    no = next(code for code in offers if code.startswith("E-"))
    for code, action in ((yes, "YES"), (no, "NO")):
        flow.simulate(offers[code], action)
        flow.drain()
    assert flow.sent == [] or all(kind != "OFFER" for kind, _ in flow.sent)  # Replies may only queue local acks.
    with flow.sessions() as session:
        statuses = {code: {item.status for item in session.scalars(select(Appointment).join(Patient).where(
            Patient.patient_code == code, Appointment.date == MONDAY))} for code in (yes, no)}
        assert statuses[yes] == {AppointmentStatus.CONFIRMED}
        assert AppointmentStatus.CONFIRMED not in statuses[no] and AppointmentStatus.PENDING_CONFIRMATION not in statuses[no]
        from services.replacement_service import find_replacement_candidates

        vacant = session.scalars(select(Appointment).join(Patient).where(
            Patient.patient_code == no, Appointment.date == MONDAY)).first()
        candidates = find_replacement_candidates(session, vacant, MONDAY, whole_visit=False)
        # Suggestions only ever come from the same centre (East), never North/South/West children.
        assert all(item["patient_code"].startswith("E-") for item in candidates)
        assert minutes(vacant.slot_index) == LENGTH["East"]


def test_zoho_rows_build_for_times_zoho_has_and_hold_times_it_lacks(flow):
    """Uses the real local lookup file (read only); no Zoho call."""
    daily_plan.plan_and_save(flow, MONDAY)
    catalog = thanal_export.default_catalog()
    branch = catalog["approved_branch_ids"][0]
    binding = next(item for item in catalog["department_bindings"] if item["branch_id"] == branch)
    built, held = defaultdict(int), defaultdict(list)
    with flow.sessions() as session:
        for item in session.scalars(select(Appointment).where(Appointment.date == MONDAY)):
            patient, doctor = session.get(Patient, item.patient_id), session.get(Therapist, item.therapist_id)
            item.status = AppointmentStatus.CONFIRMED
            source = {"branches": {branch: {"local_id": item.location_id}},
                      "therapies": {binding["consultation_id"]: {"local_id": item.department_id}}}
            context = {"identities": {"7001": patient.patient_code}, "source": source, "overrides": None,
                       "branches": {}, "requirements": {item.treatment_requirement_id}}
            centre = next(name for name, location_id in flow.centre.items() if location_id == item.location_id)
            try:
                fields = thanal_export.appointment_fields(session, item, patient, doctor, "preview", context=context)
            except ValueError as exc:
                held[centre].append(str(exc))
                continue
            assert fields["Duration"] == LENGTH[centre]
            assert fields["Date_Time"].endswith(slot_def(item.slot_index).start_time.strftime("%H:%M"))
            built[centre] += 1
        session.rollback()
    # 40-minute times already in Zoho build rows; 50- and 30-minute times are held with a clear reason.
    assert built["North"] > 0
    assert all("Zoho has no" in reason for name in ("South", "East") for reason in held[name])
    assert held["South"] and held["East"] and not built["South"] and not built["East"]
