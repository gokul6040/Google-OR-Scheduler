"""HTTP access boundary shared by all portal routes and workflow adapters."""
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
import os
import secrets
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from services.portal_auth import Accounts, Audit, User, enabled, public_user

COOKIE = "therapy_portal"
PUBLIC_ASSETS = {"/portal.js", "/portal.css", "/styles.css", "/favicon.ico", "/favicon.png", "/login-tree.png"}


def accounts() -> Accounts:
    from main_workflow.config import enabled as workflow_enabled, data_source
    if workflow_enabled() and data_source() == "test":
        from main_workflow.mount import runtime
        return Accounts(runtime().flow.engine)
    from database.db import engine
    return Accounts(engine)


def cookie_header(token: str = "") -> str:
    secure = os.getenv("PORTAL_LOCAL_HTTP", "false").lower() != "true"
    # Lax allows the existing external Zoho OAuth callback; mutations still require CSRF headers.
    return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={43200 if token else 0}"
            + ("; Secure" if secure else ""))


def shell(request) -> None:
    from web_app import WEB_DIR
    import re

    html = (WEB_DIR / "portal.html").read_text(encoding="utf-8")
    identity = getattr(request, "portal_user", None)
    path = urlsplit(request.path).path
    if identity and identity["mustChange"]:
        html = html.replace('<body class="portal-page">', '<body class="portal-page portal-password-setup">')
        html = re.sub(r'  <header class="portal-header".*?</header>\s*', '', html, flags=re.S)
    elif identity and identity["role"] == "doctor":
        html = html.replace('<body class="portal-page">', '<body class="portal-page portal-doctor-page">')
        html = re.sub(r'(<header class="portal-header">)<a href="/">.*?</a>', r'\1', html)
    if (identity and identity["role"] == "admin" and not identity["mustChange"]
            and path in {"/users", "/users/existing", "/account"}):
        # Reuse the actual workspace brand, icons and navigation rather than a second theme.
        workspace = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        sidebar = re.search(r'<aside class="sidebar".*?</aside>', workspace, re.S).group()
        sidebar = re.sub(r'<button class="nav-item(?: active)?" data-view="([^"]+)"[^>]*>(.*?)</button>',
                         lambda m: f'<a class="nav-item" href="/?view={m[1]}">{m[2]}</a>', sidebar, flags=re.S)
        icons = re.search(r'<svg class="icon-sprite".*?</svg>', workspace, re.S).group()
        body = (f'<body class="portal-workspace">{icons}<div class="app-shell">{sidebar}'
                '<div class="main-shell"><header class="topbar portal-workspace-topbar">'
                '<button class="icon-button mobile-menu" id="menuButton" aria-label="Open menu">'
                '<svg><use href="#i-menu"/></svg></button>'
                '<a href="/">Workspace</a><span aria-hidden="true">/</span><span id="portalBreadcrumb">Account</span>'
                '</header><main id="portalMain" class="portal-workspace-main" tabindex="-1">'
                '<p role="status">Loading your account…</p></main></div></div></body>')
        html = re.sub(r'<body.*?</body>', lambda _: body, html, flags=re.S)
    request.send_html(html)


def client_ip(request) -> str:
    """The address the per-address sign-in limit counts against.

    Behind Azure Container Apps every request arrives from the platform's ingress,
    so the socket address is the same for all staff and one person's wrong
    passwords would block everyone. With PORTAL_TRUST_FORWARDED_FOR=true (set only
    on the deployed app) we use the LAST X-Forwarded-For entry: the ingress appends
    the real client address there, while anything a visitor wrote themselves can
    only appear earlier in the list. Locally the socket address is used.
    """
    import ipaddress

    peer = request.client_address[0]
    if os.getenv("PORTAL_TRUST_FORWARDED_FOR", "false").strip().lower() != "true":
        return peer
    headers = request.headers.get_all("X-Forwarded-For") or []
    entries = [part.strip() for header in headers for part in header.split(",") if part.strip()]
    if not entries:
        return peer
    try:
        return str(ipaddress.ip_address(entries[-1]))
    except ValueError:
        return peer  # Malformed value: fall back to the shared limit rather than trust it.


def account_body(request) -> dict:
    from web_app import ApiError
    body = request.read_json()
    if not isinstance(body, dict):
        raise ApiError(400, "Account request must be a JSON object.")
    return body


def doctor_schedule(store: Accounts, identity: dict, day: str) -> dict:
    from database.models import Appointment, AppointmentStatus, Department, Location, Patient
    from scheduling.slot_config import slot_label
    if day not in {"today", "tomorrow"}:
        raise ValueError("Choose Today or Tomorrow.")
    target = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=day == "tomorrow")
    with store.sessions() as session:
        rows = session.execute(select(Appointment, Patient.name, Patient.patient_code,
                                      Department.name, Location.name)
            .join(Patient, Appointment.patient_id == Patient.id)
            .join(Department, Appointment.department_id == Department.id)
            .join(Location, Appointment.location_id == Location.id)
            .where(Appointment.therapist_id == identity["clinicianId"], Appointment.date == target)
            .order_by(Appointment.slot_index, Appointment.id)).all()
        items = [
            {"time": slot_label(a.slot_index), "patient": name or code, "code": code,
             "therapy": therapy, "location": location, "status": a.status.value}
            for a, name, code, therapy, location in rows]
        active = {AppointmentStatus.CONFIRMED.value, AppointmentStatus.PENDING_CONFIRMATION.value}
        cancelled = {AppointmentStatus.CANCELLED_PATIENT.value, AppointmentStatus.CANCELLED_OTHER.value,
                     AppointmentStatus.NEEDS_RESCHEDULING.value}
        return {"date": target.isoformat(),
                "sessions": [item for item in items if item["status"] in active],
                "cancellations": [item for item in items if item["status"] in cancelled]}


def dispatch(request) -> bool:
    """Return True when handled. Deny by default for doctors and anonymous users.

    Account operations raise ValueError with messages written for staff (wrong
    password, duplicate email, last admin...). The HTTP layer only maps ApiError,
    so convert them here: a failed sign-in is 401, other account errors are 400,
    instead of a generic 500 "could not complete" error.
    """
    try:
        return _dispatch(request)
    except ValueError as exc:
        from web_app import ApiError
        login = urlsplit(request.path).path == "/api/auth/login"
        raise ApiError(401 if login else 400, str(exc)) from None


def _dispatch(request) -> bool:
    if not enabled() or getattr(request, "portal_checked", False):
        return False
    from web_app import ApiError
    path = urlsplit(request.path).path
    if path in PUBLIC_ASSETS and request.command in {"GET", "HEAD"}:
        return False
    if path == "/api/health" and request.command == "GET":
        request.send_json({"ok": True})
        return True
    if path == "/webhooks/whatsapp":
        return False  # Existing provider signature/challenge verification remains required.
    store = accounts()
    jar = SimpleCookie()
    try:
        jar.load(request.headers.get("Cookie", ""))
    except Exception:
        pass
    token = jar[COOKIE].value if COOKIE in jar else ""
    identity = store.authenticate(token) if token else None
    request.portal_user = identity
    machine = os.getenv("PORTAL_MACHINE_TOKEN", "")
    if (not identity and not token and machine
            and secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + machine)
            and path.startswith(("/api/", "/workflow/api/"))
            and not path.startswith(("/api/auth/", "/api/users", "/api/doctor/"))):
        request.portal_machine = True
        request.portal_checked = True
        return False
    if path.startswith(("/api/auth/", "/api/users")) and request.command == "POST":
        if not 0 <= int(request.headers.get("Content-Length", "0")) <= 16384:
            raise ApiError(413, "Account request is too large.")
    if request.command not in {"GET", "HEAD"}:
        # Custom header forces browser preflight; no cross-origin CORS is enabled.
        origin = request.headers.get("Origin")
        if (request.headers.get("X-Portal-Request") != "1" or
                request.headers.get("Sec-Fetch-Site") == "cross-site" or
                (origin and urlsplit(origin).netloc.lower() != request.headers.get("Host", "").lower())):
            raise ApiError(403, "Use the portal to submit this request.")
    if path == "/api/auth/login" and request.command == "POST":
        body = account_body(request)
        token, identity = store.login(body.get("email", ""), body.get("password", ""), client_ip(request))
        request.portal_cookie = cookie_header(token)
        request.send_json({"user": identity})
        return True
    if path == "/api/auth/me" and request.command == "GET":
        request.send_json({"user": identity})
        return True
    if path in {"/login", "/account"} and request.command == "GET":
        shell(request)
        return True
    if path == "/api/auth/logout" and request.command == "POST":
        # Consume the JSON body before accepting another HTTP/1.1 request.
        # Also allow expired/already-ended sessions to return to sign-in cleanly.
        account_body(request)
        store.logout(token)
        request.portal_cookie = cookie_header()
        request.send_json({"ok": True})
        return True
    if not identity:
        if request.command == "GET" and not path.startswith(("/api/", "/workflow/api/", "/test/api/")):
            shell(request)
            return True
        raise ApiError(401, "Please sign in.")
    if path == "/api/auth/password" and request.command == "POST":
        body = account_body(request)
        store.change_password(identity, body.get("current", ""), body.get("password", ""))
        request.portal_cookie = cookie_header()
        request.send_json({"ok": True})
        return True
    if identity["mustChange"]:
        if request.command == "GET" and not path.startswith("/api/"):
            shell(request)
            return True
        raise ApiError(403, "Change your temporary password first.")
    if identity["role"] == "doctor":
        if path == "/api/doctor/schedule" and request.command == "GET":
            from urllib.parse import parse_qs
            day = parse_qs(urlsplit(request.path).query).get("day", ["today"])[0]
            request.send_json(doctor_schedule(store, identity, day))
            return True
        if path in {"/", "/doctor", "/index.html"} and request.command == "GET":
            shell(request)
            return True
        raise ApiError(403, "Your account can only view your own Today and Tomorrow sessions.")
    if path in {"/users", "/users/existing"} and request.command == "GET":
        shell(request)
        return True
    if path == "/api/users" and request.command == "GET":
        from database.models import Therapist
        with store.sessions() as session:
            request.send_json({"users": [public_user(u) for u in session.scalars(select(User).order_by(User.name))],
                # Active clinicians only; therapy and centre tell same-named clinicians apart in the picker.
                "clinicians": [{"id": d.id, "name": d.name, "department": d.department.name if d.department else "",
                                "location": d.location.name if d.location else ""} for d in session.scalars(
                    select(Therapist).where(Therapist.active.is_(True)).order_by(Therapist.name))]})
        return True
    if path == "/api/users" and request.command == "POST":
        body = account_body(request)
        temporary = store.create(body.get("name", ""), body.get("email", ""), body.get("role", ""),
                                 body.get("clinicianId"), actor=identity["email"])
        request.send_json({"temporaryPassword": temporary}, 201)
        return True
    import re
    edit = re.fullmatch(r"/api/users/(\d+)/edit", path)
    if edit and request.command == "POST":
        body = account_body(request)
        store.edit(int(edit[1]), body.get("name"), body.get("email"), body.get("clinicianId"), identity["email"])
        request.send_json({"ok": True, "reauthenticate": int(edit[1]) == identity["id"]})
        return True
    match = re.fullmatch(r"/api/users/(\d+)/(reset|activate|deactivate)", path)
    if match and request.command == "POST":
        request.read_json()
        temporary = store.update(int(match[1]), match[2], identity["email"])
        request.send_json({"ok": True, "temporaryPassword": temporary,
                           "reauthenticate": int(match[1]) == identity["id"]})
        return True
    if path == "/api/users/audit" and request.command == "GET":
        with store.sessions() as session:
            request.send_json({"events": [{"at": a.at.isoformat(), "actor": a.actor, "action": a.action}
                for a in session.scalars(select(Audit).order_by(Audit.id.desc()).limit(200))]})
        return True
    request.portal_checked = True
    return False
