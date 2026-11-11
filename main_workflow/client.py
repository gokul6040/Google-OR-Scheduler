"""Authenticated CLI access to the existing app, without test-token fallbacks."""

import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from flow_test.reset_live import BASE_URL
from main_workflow.config import WorkflowConfig


def app_base_url() -> str:
    """The app address the CLI and daily job talk to.

    Unset, it is the original Azure test app. A deployment (for example the client's
    own Azure app) sets MAIN_WORKFLOW_APP_URL explicitly; it must be a bare https://
    origin, or plain http on this computer for local verification servers.
    """
    base = (os.getenv("MAIN_WORKFLOW_APP_URL", "").strip() or BASE_URL).rstrip("/")
    target = urlsplit(base)
    loopback = target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1"}
    if (target.username or target.password or target.query or target.fragment or target.path
            or not target.hostname or not (target.scheme == "https" or loopback)):
        raise ValueError("Set MAIN_WORKFLOW_APP_URL to the app's https:// address (or a loopback verification server)")
    return base


def api(config: WorkflowConfig, operation: str, body: dict | None = None, *, timeout: float = 180) -> dict:
    base = app_base_url()
    if operation not in {"status", "import", "export/preview", "export/prepare", "export/ack", "export/failures",
                         "export/finish", "export/push"}:
        raise ValueError("Unsupported operations request")
    request = Request(base + "/api/zoho/" + operation,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {config.admin_token}", "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", "Operations request failed")
        except (ValueError, AttributeError):
            message = f"Operations returned HTTP {exc.code}"
        raise ValueError(message) from None

