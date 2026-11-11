"""Mount the isolated flow in the existing HTTP server at /test."""

import copy
import os
import secrets
import threading

from services.whatsapp_worker import embedded_worker_enabled
from urllib.parse import urlsplit

from config import load_local_env_file
from flow_test.service import FlowTest, ROOT, load_recipient_names, load_recipients
from flow_test.web_app import handler_for


_lock = threading.Lock()
_runtime = None


class MountedFlow:
    """One test worker and database per application process."""

    def __init__(self):
        load_local_env_file(ROOT / ".env")
        live = os.getenv("FLOW_TEST_LIVE", "false").lower() == "true"
        prefix = "FLOW_TEST" if live else "FLOW_TEST_PREVIEW"
        contacts = ROOT / "recipients.local.json"
        has_contacts = bool(os.getenv("FLOW_TEST_RECIPIENTS_B64", "").strip()) or contacts.exists()
        self.flow = FlowTest(
            os.getenv(f"{prefix}_DATABASE_URL") or None, live=live,
            database_schema=os.getenv(f"{prefix}_DATABASE_SCHEMA") or None,
            recipients=load_recipients(contacts) if has_contacts else {},
            recipient_names=load_recipient_names(contacts) if has_contacts else {},
        )
        # /test serves the shared staff screens, which need only the selected date.
        self.flow.compact_state = True
        self.token = os.getenv("FLOW_TEST_ADMIN_TOKEN", "").strip()
        if not self.token:
            token_file = ROOT / "data" / "admin.token"
            token_file.parent.mkdir(exist_ok=True)
            if not token_file.exists():
                token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            self.token = token_file.read_text(encoding="utf-8").strip()
        self.stop = threading.Event()
        from flow_test.preview_store import PreviewStore
        self.previews = PreviewStore(self.flow)  # Shared by every app copy.
        self.worker = None
        if live and embedded_worker_enabled():
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    def work(self):
        while not self.stop.wait(1):
            try:
                self.flow.expire_offers()
                self.flow.process_batch()
            except Exception:
                print("Test worker paused one cycle after a database error")

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=25)
        self.flow.engine.dispose()


def runtime():
    """Initialize only on test API use; the main database is never selected."""
    global _runtime
    from main_workflow.config import data_source, enabled

    if enabled() and data_source() == "test":
        from main_workflow.mount import runtime as main_runtime

        return main_runtime()
    with _lock:
        if _runtime is None:
            _runtime = MountedFlow()
        return _runtime


def close():
    global _runtime
    with _lock:
        if _runtime is not None:
            _runtime.close()
            _runtime = None


def route_webhook(payload):
    """Send registered test-contact events to test storage, retaining others."""
    if os.getenv("FLOW_TEST_LIVE", "false").lower() != "true":
        return payload
    instance = runtime()
    if not instance.flow.live:
        return payload
    return route_payload(payload, instance)


def webhook_numbers(payload: dict) -> set[str]:
    """Sender and receipt numbers in one callback; only these need a contact check."""
    return {str(item.get(field))
            for entry in payload.get("entry", []) if isinstance(entry, dict)
            for change in entry.get("changes", []) if isinstance(change, dict)
            for kind, field in (("messages", "from"), ("statuses", "recipient_id"))
            for item in change.get("value", {}).get(kind, []) if isinstance(item, dict) and item.get(field)}


def route_payload(payload: dict, instance) -> dict:
    """Route a signed callback to the selected retained test runtime."""
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    allowed = instance.flow.contacts_for(webhook_numbers(payload))
    remaining = copy.deepcopy(payload)
    selected = copy.deepcopy(payload)
    for original_entry, test_entry in zip(remaining.get("entry", []), selected.get("entry", [])):
        for original_change, test_change in zip(original_entry.get("changes", []), test_entry.get("changes", [])):
            value, test_value = original_change.get("value", {}), test_change.get("value", {})
            matches = bool(phone_id and value.get("metadata", {}).get("phone_number_id") == phone_id)
            for key, sender in (("messages", "from"), ("statuses", "recipient_id")):
                items = value.get(key, [])
                test_value[key] = [item for item in items if matches and item.get(sender) in allowed]
                value[key] = [item for item in items if not (matches and item.get(sender) in allowed)]
    # The caller has already checked the signature over the complete raw body.
    instance.flow.receive(selected)
    return remaining


def workspace_html():
    """Serve the actual application UI; only assets and the test marker differ."""
    shell = (ROOT.parent / "web" / "index.html").read_text(encoding="utf-8")
    shell = shell.replace('<title>EIC Therapy Scheduler</title>', '<title>EIC Therapy Scheduler · Test</title>')
    shell = shell.replace('href="/styles.css"', 'href="/test/shared.css"')
    shell = shell.replace('</head>', '<link rel="stylesheet" href="/test/workspace.css"></head>')
    shell = shell.replace('<small>Local workspace</small>', '<small>Fictional test workspace</small>')
    shell = shell.replace('src="/app.js"', 'src="/test/shared.js"')
    shell = shell.replace('src="/whatsapp_controls.js"', 'src="/test/whatsapp_controls.js"')
    shell = shell.replace('<script src="/test/shared.js"', '<script src="/test/workspace.js" defer></script><script src="/test/shared.js"')
    return shell


def dispatch(request):
    """Explicit routes only; /test APIs cannot fall through to production APIs."""
    parsed = urlsplit(request.path)
    if request.command == "GET":
        if parsed.path in {"/test", "/test/"}:
            request.send_html(workspace_html())
            return
        assets = {
            "/test/shared.css": (ROOT.parent / "web" / "styles.css", "text/css"),
            "/test/shared.js": (ROOT.parent / "web" / "app.js", "application/javascript"),
            "/test/whatsapp_controls.js": (ROOT.parent / "web" / "whatsapp_controls.js", "application/javascript"),
            "/test/workspace.css": (ROOT / "web" / "workspace.css", "text/css"),
            "/test/workspace.js": (ROOT / "web" / "workspace.js", "application/javascript"),
        }
        if parsed.path in assets:
            path, mime = assets[parsed.path]
            data = path.read_bytes()
            request.send_response(200)
            request.send_header("Content-Type", f"{mime}; charset=utf-8")
            request.send_header("Content-Length", str(len(data)))
            request.send_header("Cache-Control", "no-cache")
            request.send_header("X-Content-Type-Options", "nosniff")
            request.end_headers()
            request.wfile.write(data)
            return
    if not parsed.path.startswith("/test/api/"):
        request.send_json({"error": "Test route not found"}, 404)
        return
    try:
        instance = runtime()
    except Exception:
        request.send_json({"error": "Test configuration is incomplete. Check test database, recipients and live settings."}, 503)
        return
    from flow_test.shared_ui import dispatch_shared_api

    if dispatch_shared_api(request, instance):
        return
    bound = handler_for(instance.flow, instance.token)
    adapter = bound.__new__(bound)
    adapter.__dict__.update(request.__dict__)
    # Header buffers belong to each response. Sharing the original list would
    # repeat old headers on subsequent requests over a keep-alive connection.
    adapter._headers_buffer = []
    adapter.path = request.path.removeprefix("/test")
    # Adapter writes through the same HTTP/1.1 connection; no second server.
    adapter.protocol_version = request.protocol_version
    if request.command == "GET":
        adapter.do_GET()
    else:
        adapter.do_POST()
    request.close_connection = adapter.close_connection
