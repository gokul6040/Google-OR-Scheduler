"""Authenticated CLI access to the existing app, without test-token fallbacks."""

import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from flow_test.reset_live import BASE_URL
from main_workflow.config import WorkflowConfig


def api(config: WorkflowConfig, operation: str, body: dict | None = None) -> dict:
    base = os.getenv("MAIN_WORKFLOW_APP_URL", BASE_URL).rstrip("/")
    target = urlsplit(base)
    if (target.username or target.query or target.fragment or target.path or not (
            (target.scheme == "https" and target.hostname == urlsplit(BASE_URL).hostname)
            or (target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1"}))):
        raise ValueError("Use the existing application host or a loopback verification server")
    if operation not in {"status", "import", "export/preview", "export/prepare", "export/ack", "export/finish"}:
        raise ValueError("Unsupported operations request")
    request = Request(base + "/api/zoho/" + operation,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {config.admin_token}", "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", "Operations request failed")
        except (ValueError, AttributeError):
            message = f"Operations returned HTTP {exc.code}"
        raise ValueError(message) from None

