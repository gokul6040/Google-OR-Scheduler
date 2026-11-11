"""Run the existing UI handlers against the selected isolated test session."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime
import re
import secrets
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from config import SolverSettings

GET_ROUTES = (
    r"/api/schedules/jobs/\d+",
    r"/api/(overview|schedule|patients|therapists|operations|health)",
    r"/api/zoho/(status|draft)",
    r"/api/patients/\d+/preferences",
    r"/api/patients/\d+/activation",
    r"/api/appointments/\d+/(replacement-candidates|reservation-context)",
)
POST_ROUTES = (
    r"/api/reset",
    r"/api/zoho/import",
    r"/api/zoho/export/(preview|prepare|ack|failures|finish|push)",
    r"/api/absences",
    r"/api/patients/\d+/preferences",
    r"/api/patients/\d+/status",
    r"/api/appointments/\d+/(cancel|move|reservation-preview|reserve)",
    r"/api/calendar/(reservation-preview|reserve)",
    r"/api/schedules/generate",
    r"/api/schedules/[0-9a-f-]+/save",
)


def generate_daily_preview(request, instance) -> None:
    """Reuse the daily flow's inputs, with the shared UI's review/save contract."""
    import web_app

    body = request.read_json()
    target = web_app.parse_date(body.get("start"), "start")
    seconds = web_app.parse_search_seconds(body.get("searchSeconds"))
    if body.get("planningMode", "day") != "day":
        raise web_app.ApiError(400, "This workflow plans one selected day at a time")
    if type(body.get("background", False)) is not bool:
        raise web_app.ApiError(400, "background must be true or false")
    # Production always gets the agreed five-minute maximum, even when an old
    # browser sends the former 20-second quick-preview selection.
    settings = replace(instance.flow.solver_settings, max_time_seconds=(
        300.0 if not instance.flow.is_test else seconds))
    if body.get("background") or not instance.flow.is_test:
        from flow_test.planning_tasks import submit_preview
        request.send_json(submit_preview(instance, target, body.get("patients"), settings), 202)
        return
    request.send_json(build_daily_preview(instance, target, body.get("patients"), settings), 201)


def build_daily_preview(instance, target: date, patient_codes: list[str] | None, settings: SolverSettings) -> dict:
    """Generate an unsaved preview; usable by synchronous tests and background jobs."""
    import web_app

    try:
        result, pinned = instance.flow.preview_daily(
            target, patient_codes, settings=settings,
        )
    except ValueError as exc:
        raise web_app.ApiError(400, str(exc)) from exc
    # The solver counts the whole week. Only selected-day rows may be displayed
    # or passed to a one-day save; including other pinned days would duplicate
    # them because the existing saver checks only its requested date range.
    day_result = replace(result, appointments=[a for a in result.appointments if a.date == target])
    new_count = sum(a not in pinned for a in day_result.appointments)
    offer_on_save = target == instance.flow.next_offer_date()
    preview_id = str(uuid4())
    instance.previews[preview_id] = {
        "result": day_result, "start": target, "horizon_days": 1,
        "created": datetime.utcnow(),
        "offer_on_save": offer_on_save,
    }
    context = web_app.WORKSPACE_CONTEXT.set({"sessions": instance.flow.sessions,
        "previews": instance.previews, "flow": instance.flow,
        "admin_user": getattr(instance.flow, "admin_id", "scheduler_manager")})
    try:
        payload = web_app.serialize_preview(day_result, target, target)
    finally:
        web_app.WORKSPACE_CONTEXT.reset(context)
    payload.update({
        "previewId": preview_id, "planningStart": target.isoformat(),
        "planningMode": "day", "planningDays": 1,
        "newSessions": new_count, "scheduledOnSelectedDay": len(day_result.appointments),
        "autoOfferOnSave": offer_on_save, "liveMessaging": getattr(instance.flow, "messaging_enabled", instance.flow.live),
        "heldCentres": result.statistics.get("held_centres", []),
        "childrenNotPlanned": result.statistics.get("held_children", []),
        "messagingPaused": instance.flow.live and not getattr(instance.flow, "messaging_enabled", instance.flow.live),
    })
    # Keep the response alongside the draft, not in a long-lived queue payload.
    preview = instance.previews[preview_id]
    preview["response"] = payload
    instance.previews[preview_id] = preview
    return payload


def save_daily_preview(request, instance, preview_id: str) -> None:
    """Publish tomorrow's calendar as part of its save, preserving safe retries."""
    import web_app

    request.read_json()
    preview = instance.previews.get(preview_id)
    if preview is None:
        raise web_app.ApiError(404, "This preview has expired. Generate it again.")
    if "saved_response" in preview:
        request.send_json(preview["saved_response"])
        return
    try:
        response = instance.flow.save_calendar(
            preview["result"], preview["start"],
            expected_offer_on_save=preview["offer_on_save"],
        )
    except ValueError as exc:
        raise web_app.ApiError(409, str(exc)) from exc
    preview["saved_response"] = response
    # Write back: previews may live in the shared database, where `get` returns a copy.
    instance.previews[preview_id] = preview
    request.send_json(response)


def dispatch_shared_api(request, instance, *, prefix: str = "/test") -> bool:
    """Allow local UI workflows with a request-scoped session and previews."""
    path = urlsplit(request.path).path.removeprefix(prefix)
    patterns = GET_ROUTES if request.command == "GET" else POST_ROUTES
    local_route = any(re.fullmatch(pattern, path) for pattern in patterns)
    assistant_status = path == "/api/assistant/status" and request.command == "GET"
    if not local_route and not assistant_status:
        return False
    if not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {instance.token}"):
        request.close_connection = True
        request.send_json({"error": "Enter the test admin key to open this workspace."}, 401)
        return True
    if assistant_status:
        request.send_json({"configured": False})
        return True
    import web_app

    planning_status = re.fullmatch(r"/api/schedules/jobs/(\d+)", path)
    if request.command == "GET" and planning_status:
        from flow_test.planning_tasks import preview_status
        request.send_json(preview_status(instance, int(planning_status[1])))
        return True

    # A background solver takes its own short snapshot lock. Never hold the
    # request's outer workflow lock for the lifetime of a synchronous solve.
    if request.command == "POST" and path == "/api/schedules/generate":
        generate_daily_preview(request, instance)
        return True

    if path == "/api/zoho/import" and getattr(instance.flow, "remote_worker", False):
        from flow_test.zoho_routes import dispatch_zoho

        # The remote worker uses the same database mutex. Holding it while
        # waiting for that worker's HTTP response would deadlock the import.
        # Its import transaction takes the shared lock; local refresh takes its
        # own lock only after the remote request has finished.
        getattr(instance, "zoho_dispatch", dispatch_zoho)(request, instance, path)
        return True

    if path == "/api/zoho/export/push":
        from flow_test.zoho_calendar import dispatch_calendar_push
        # State helpers take their own locks. External HTTP must not block
        # incoming replies or other calendar reads through the workflow lock.
        getattr(instance, "calendar_push", dispatch_calendar_push)(request, instance)
        return True

    status_change = request.command == "POST" and re.fullmatch(r"/api/patients/\d+/status", path)
    # Match the worker's lock order so a send is settled before deciding how to handle its offer.
    with (instance.flow.sender_lock if status_change else nullcontext()), instance.flow.lock:
        if not hasattr(instance, "previews"):
            instance.previews = {}
        context = web_app.WORKSPACE_CONTEXT.set({
            "sessions": instance.flow.sessions, "previews": instance.previews,
            "admin_user": getattr(instance.flow, "admin_id", "scheduler_manager"),
            "flow": instance.flow,
        })
        adapter = web_app.TherapySchedulerHandler.__new__(web_app.TherapySchedulerHandler)
        adapter.__dict__.update(request.__dict__)
        adapter.path = request.path.removeprefix(prefix)
        adapter._headers_buffer = []
        try:
            save = re.fullmatch(r"/api/schedules/([0-9a-f-]+)/save", path)
            reservation = re.fullmatch(r"/api/appointments/(\d+)/reserve", path)
            if path.startswith("/api/zoho/export/"):
                from flow_test.zoho_export import dispatch_export
                dispatch_export(adapter, instance, path.rsplit("/", 1)[-1])
            elif path in {"/api/zoho/import", "/api/zoho/status", "/api/zoho/draft"}:
                from flow_test.zoho_routes import dispatch_zoho
                getattr(instance, "zoho_dispatch", dispatch_zoho)(adapter, instance, path)
            elif request.command == "POST" and path == "/api/reset":
                body = adapter.read_json()
                try:
                    result = instance.flow.reset_test_data(body.get("confirmation"))
                except ValueError as exc:
                    raise web_app.ApiError(409, str(exc)) from exc
                instance.previews.clear()
                adapter.send_json(result)
            elif request.command == "POST" and save:
                save_daily_preview(adapter, instance, save[1])
            elif request.command == "POST" and reservation:
                body = adapter.read_json()
                if body.get("mode") != "with_messaging":
                    adapter.admin_reservation(int(reservation[1]), body, save=True)
                else:
                    patient_id = body.get("patientId")
                    if type(patient_id) is not int or patient_id <= 0:
                        raise web_app.ApiError(400, "Choose a patient from the patient list")
                    for name in ("wholeVisit", "overrideWarnings"):
                        if name in body and type(body[name]) is not bool:
                            raise web_app.ApiError(400, f"{name} must be true or false")
                    try:
                        result = instance.flow.reserve_with_message(
                            int(reservation[1]), patient_id=patient_id,
                            request_id=body.get("requestId"), preview_token=body.get("previewToken"),
                            whole_visit=body.get("wholeVisit", True),
                            override_warnings=body.get("overrideWarnings", False),
                        )
                    except ValueError as exc:
                        raise web_app.ApiError(409, str(exc)) from exc
                    adapter.send_json(result)
            elif request.command == "GET" and path == "/api/overview":
                local_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
                adapter.send_json({**web_app.overview_payload(local_today), "planningMode": "day"})
            elif request.command == "GET":
                adapter.do_GET()
            else:
                adapter.do_POST()
        finally:
            web_app.WORKSPACE_CONTEXT.reset(context)
            request.close_connection = adapter.close_connection
    return True
