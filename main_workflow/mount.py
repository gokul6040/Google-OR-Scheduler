"""Mount opt-in daily operations beside the unchanged main and test interfaces."""

import copy
import ipaddress
import os
from pathlib import Path
import secrets
import threading

from services.whatsapp_worker import embedded_worker_enabled
from typing import Any
from urllib.parse import urlsplit

from flow_test.shared_ui import dispatch_shared_api
from flow_test.web_app import handler_for
from flow_test.zoho_calendar import _push_lock
from flow_test.zoho_export import acknowledge_export, export_preview, finish_export, prepare_export
from flow_test.zoho_push import REQUIRED_SCOPES, run_push
from flow_test.zoho_routes import dispatch_zoho
from main_workflow.config import WorkflowConfig, data_source, enabled, local_no_auth, requires_access_key
from main_workflow.provider import ZohoProvider
from main_workflow.service import MainWorkflow
from services.zoho_creator_service import refresh_access_token


ROOT = Path(__file__).resolve().parent
_lock = threading.Lock()
_runtime = None


class MainRuntime:
    def __init__(self) -> None:
        from database.db import engine

        self.config = WorkflowConfig.from_environment()
        self.flow = MainWorkflow(engine, self.config)
        self.token = self.config.admin_token
        self.provider = ZohoProvider(self.config)
        self.previews = {}
        self.stop = threading.Event()
        self.worker = None
        if self.flow.live and embedded_worker_enabled():
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    def work(self) -> None:
        while not self.stop.wait(1):
            try:
                self.flow.expire_offers()
                self.flow.queue_template_reminders()
                self.flow.process_batch()
            except Exception:
                print("Daily operations worker paused one cycle; inspect the queue")

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=25)

    @staticmethod
    def zoho_dispatch(request: Any, instance: Any, path: str) -> None:
        if path == "/api/zoho/import":
            body = request.read_json()
            if body.get("source") != instance.config.source_name:
                import web_app

                raise web_app.ApiError(409, "The import source does not match this main workflow")
            if instance.config.registration_report and "All_Registrations" not in body.get("reports", {}):
                import web_app

                raise web_app.ApiError(409, "This source requires All_Registrations with every import")
            request.read_json = lambda: body
        dispatch_zoho(request, instance, path)

    @staticmethod
    def calendar_push(request: Any, instance: Any) -> None:
        import web_app

        target = web_app.parse_date(request.read_json().get("date"), "date")
        if not _push_lock.acquire(blocking=False):
            raise web_app.ApiError(409, "A Zoho push is already running; wait and refresh")
        try:
            settings = instance.provider.settings()
            instance.provider.verify(settings)
            connection = refresh_access_token(settings)
            if not REQUIRED_SCOPES <= set(connection.get("scope", "").split()):
                raise ValueError("The main Zoho connection needs read/create/update and form-definition permissions")

            def exchange(operation, body):
                if operation == "preview":
                    return export_preview(instance.flow, target)
                if operation == "prepare":
                    result = prepare_export(instance.flow, target)
                    instance.previews.clear()
                    return result
                if operation == "ack":
                    return acknowledge_export(instance.flow, body["batchId"], body["appointmentId"],
                                              body["fingerprint"], body["recordId"])
                return finish_export(instance.flow, body["batchId"])

            result = run_push(settings, connection["access_token"], "", "", target, apply=True,
                              exchange=exchange, verify=instance.provider.verify, key_prefix="main-")
        except (ValueError, OSError):
            raise web_app.ApiError(409, "Zoho push could not complete. Check the configured main form, unique Scheduler_Key and permissions; any prepared batch is retained") from None
        finally:
            _push_lock.release()
        request.send_json(result)


def runtime() -> MainRuntime:
    global _runtime
    if not enabled():
        raise ValueError("The main daily workflow is disabled")
    with _lock:
        if _runtime is None:
            if data_source() == "test":
                from main_workflow.retained_data import TestDataRuntime

                _runtime = TestDataRuntime()
            else:
                _runtime = MainRuntime()
        return _runtime


def close() -> None:
    global _runtime
    with _lock:
        if _runtime is not None:
            _runtime.close()
            _runtime = None


def workspace_html() -> str:
    shell = (ROOT.parent / "web/index.html").read_text(encoding="utf-8")
    for before, after in (("/styles.css", "/workflow/shared.css"), ("/app.js", "/workflow/shared.js"),
                          ("/whatsapp_controls.js", "/workflow/whatsapp_controls.js")):
        shell = shell.replace(f'"{before}"', f'"{after}"')
    shell = shell.replace("</head>", '<link rel="stylesheet" href="/workflow/workspace.css"></head>')
    return shell.replace('<script src="/workflow/shared.js"',
                         '<script src="/workflow/workspace.js" defer></script><script src="/workflow/shared.js"')


def local_request_allowed(request: Any) -> bool:
    """Accept only loopback requests with a matching host and browser origin."""
    try:
        if not ipaddress.ip_address(request.client_address[0]).is_loopback:
            return False
        server_host, server_port = request.server.server_address[:2]
        if not ipaddress.ip_address(server_host).is_loopback:
            return False
        host = request.headers.get("Host", "")
        parsed = urlsplit("http://" + host)
        if (parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != server_port
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            return False
        origin = request.headers.get("Origin")
        if origin and origin.lower() != ("http://" + host).lower():
            return False
        if request.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}:
            return False
        return True
    except (ValueError, AttributeError, IndexError, TypeError):
        return False


def dispatch(request: Any, *, prefix: str = "/workflow") -> None:
    path = urlsplit(request.path).path
    if not enabled():
        request.close_connection = True
        request.send_json({"error": "The main daily workflow is disabled"}, 404)
        return
    if request.command == "GET" and path in {"/workflow", "/workflow/"}:
        request.send_html(workspace_html())
        return
    assets = {"/workflow/shared.js": ROOT.parent / "web/app.js",
              "/workflow/whatsapp_controls.js": ROOT.parent / "web/whatsapp_controls.js",
              "/workflow/shared.css": ROOT.parent / "web/styles.css",
              "/workflow/workspace.css": ROOT.parent / "flow_test/web/workspace.css",
              "/workflow/workspace.js": ROOT / "workspace.js"}
    if request.command == "GET" and path in assets:
        file = assets[path]
        request.send_response(200)
        request.send_header("Content-Type", "text/css; charset=utf-8" if file.suffix == ".css" else "application/javascript; charset=utf-8")
        content = file.read_bytes()
        request.send_header("Content-Length", str(len(content)))
        request.send_header("Cache-Control", "no-cache")
        request.end_headers()
        request.wfile.write(content)
        return
    if not path.startswith(prefix + "/api/"):
        request.close_connection = True
        request.send_json({"error": "Workflow route not found"}, 404)
        return
    local_password_free = not prefix and local_no_auth()
    password_free = not prefix and (local_no_auth() or not requires_access_key())
    if local_password_free and not local_request_allowed(request):
        request.close_connection = True
        request.send_json({"error": "Local access only"}, 403)
        return
    if password_free and not local_password_free and request.command != "GET":
        origin = request.headers.get("Origin")
        host = request.headers.get("Host", "").lower()
        if (request.headers.get("Sec-Fetch-Site") == "cross-site"
                or (origin and urlsplit(origin).netloc.lower() != host)):
            request.close_connection = True
            request.send_json({"error": "Use the main application to make this change"}, 403)
            return
    if password_free:
        instance = runtime()
        if "Authorization" in request.headers:
            del request.headers["Authorization"]
        request.headers["Authorization"] = f"Bearer {instance.token}"
    elif data_source() == "test":
        from main_workflow.retained_data import access_key

        key = access_key()
    else:
        key = os.getenv("MAIN_WORKFLOW_ADMIN_TOKEN", "")
    if not password_free and (not key or not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {key}")):
        request.close_connection = True
        request.send_json({"error": "Enter the operations access key"}, 401)
        return
    relative = path.removeprefix(prefix) if prefix else path
    if relative in {"/api/reset", "/api/generate"} or (relative == "/api/simulate" and data_source() != "test"):
        request.close_connection = True
        request.send_json({"error": "This action is unavailable in main operations"}, 404)
        return
    instance = instance if password_free else runtime()
    if getattr(instance.flow, "remote_worker", False):
        instance.flow.refresh_source()
        if request.command == "POST" and relative == "/api/process":
            request.read_json()
            request.send_json({"processed": 0, "worker": "Azure test sender"})
            return
    if (request.command == "POST" and relative == "/api/process" and instance.flow.live
            and not getattr(instance.flow, "messaging_enabled", instance.flow.live)):
        request.read_json()
        request.send_json({"processed": instance.flow.drain(), "outboundMessaging": "OFF"})
        return
    if dispatch_shared_api(request, instance, prefix=prefix):
        return
    if (request.command, relative) not in {
            ("GET", "/api/state"), ("POST", "/api/publish"), ("POST", "/api/attendance"),
            ("POST", "/api/release"), ("POST", "/api/resolve"), ("POST", "/api/process"),
            ("POST", "/api/simulate"), ("POST", "/api/resume-sending")}:
        request.close_connection = True
        request.send_json({"error": "Workflow route not found"}, 404)
        return
    bound = handler_for(instance.flow, instance.token)
    adapter = bound.__new__(bound)
    adapter.__dict__.update(request.__dict__)
    adapter._headers_buffer = []
    adapter.path = request.path.removeprefix(prefix) if prefix else request.path
    (adapter.do_GET if request.command == "GET" else adapter.do_POST)()
    request.close_connection = adapter.close_connection


def route_webhook(payload: dict) -> dict:
    """Route main references first; preserve shared contact windows and receipts."""
    if not enabled():
        return payload
    instance = runtime()
    if data_source() == "test":
        from flow_test.mount import route_payload

        return route_payload(payload, instance) if instance.flow.live else payload
    if not instance.flow.live:
        return payload
    contacts = instance.flow.contact_numbers()
    business = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    test_contacts = set()
    if os.getenv("FLOW_TEST_LIVE", "false").lower() == "true":
        from flow_test.mount import runtime as test_runtime

        test_contacts = test_runtime().flow.contact_numbers()
    remaining, selected = copy.deepcopy(payload), copy.deepcopy(payload)
    for original_entry, main_entry in zip(remaining.get("entry", []), selected.get("entry", [])):
        for original_change, main_change in zip(original_entry.get("changes", []), main_entry.get("changes", [])):
            value, chosen = original_change.get("value", {}), main_change.get("value", {})
            matches = business and value.get("metadata", {}).get("phone_number_id") == business
            chosen["messages"], chosen["statuses"] = [], []
            kept = []
            for message in value.get("messages", []):
                button = message.get("button", {}).get("payload") or message.get("interactive", {}).get("button_reply", {}).get("id", "")
                if matches and message.get("from") in contacts and not str(button).startswith("ft:"):
                    chosen["messages"].append(message)
                    if message.get("from") in test_contacts and not str(button).startswith("mw:"):
                        # A shared parent's START may also open the test contact window.
                        kept.append(message)
                else:
                    kept.append(message)
            value["messages"] = kept
            chosen["statuses"] = [status for status in value.get("statuses", [])
                                  if matches and status.get("recipient_id") in contacts]
            value["statuses"] = [status for status in value.get("statuses", [])
                                 if not (matches and status.get("recipient_id") in contacts)
                                 or status.get("recipient_id") in test_contacts]
    instance.flow.receive(selected)
    return remaining


def dispatch_root(request: Any) -> bool:
    """Serve the full workflow on the actual main / and /api routes."""
    if not enabled():
        return False
    path = urlsplit(request.path).path
    if request.command == "GET" and path in {"/", "/index.html"}:
        shell = (ROOT.parent / "web/index.html").read_text(encoding="utf-8")
        shell = shell.replace("</head>", '<link rel="stylesheet" href="/main-workspace.css"></head>')
        shell = shell.replace('<script src="/app.js"',
            '<script src="/main-workflow-config.js" defer></script><script src="/main-workspace.js" defer></script><script src="/app.js"')
        for name in ("styles.css", "app.js", "whatsapp_controls.js"):
            revision = (ROOT.parent / "web" / name).stat().st_mtime_ns
            shell = shell.replace(f'"/{name}"', f'"/{name}?v={revision}"')
        request.send_html(shell)
        return True
    assets = {"/main-workspace.js": ROOT / "workspace.js",
              "/main-workspace.css": ROOT.parent / "flow_test/web/workspace.css"}
    if request.command == "GET" and (path in assets or path == "/main-workflow-config.js"):
        if path == "/main-workflow-config.js":
            import json

            content = ("window.therapyWorkspace=" + json.dumps({"apiPrefix": "", "dataSource": data_source(), "localNoAuth": local_no_auth(),
                       "requiresAccessKey": requires_access_key() and not local_no_auth(),
                       "live": getattr(runtime().flow, "messaging_enabled", runtime().flow.live)}) + ";").encode()
        else:
            content = assets[path].read_bytes()
        request.send_response(200)
        request.send_header("Content-Type", "text/css; charset=utf-8" if path.endswith(".css") else "application/javascript; charset=utf-8")
        request.send_header("Cache-Control", "no-store")
        request.send_header("Content-Length", str(len(content)))
        request.end_headers()
        request.wfile.write(content)
        return True
    if path.startswith("/api/") and path != "/api/workflow/status":
        dispatch(request, prefix="")
        return True
    return False
