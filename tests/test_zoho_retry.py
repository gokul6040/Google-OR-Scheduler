"""Retry timing, provider cooldowns and non-retryable failures."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from services import zoho_retry


def test_backoff_is_bounded_and_increases(monkeypatch):
    delays, calls = [], []
    monkeypatch.setattr(zoho_retry.time, "sleep", delays.append)
    monkeypatch.setattr(zoho_retry.random, "uniform", lambda *_: 0)

    def fail():
        calls.append(True)
        raise URLError("offline")

    with pytest.raises(URLError):
        zoho_retry.retry_read(fail)
    assert len(calls) == 4 and delays == [1, 2, 4]


@pytest.mark.parametrize("status,wait,attempts", [(429, "3", 2), (503, "120", 1), (401, "0", 1), (400, "0", 1)])
def test_retry_after_and_permanent_errors(monkeypatch, status, wait, attempts):
    delays, calls = [], []
    monkeypatch.setattr(zoho_retry.time, "sleep", delays.append)

    def request():
        calls.append(True)
        if len(calls) == 1:
            raise HTTPError("http://localhost", status, "failure", {"Retry-After": wait}, BytesIO(b"{}"))
        return "ok"

    if attempts == 2:
        assert zoho_retry.retry_read(request) == "ok" and delays == [3]
    else:
        with pytest.raises(HTTPError):
            zoho_retry.retry_read(request)
        assert not delays
    assert len(calls) == attempts


def test_retry_after_http_date_and_invalid_values():
    future = datetime.now(timezone.utc) + timedelta(seconds=90)
    assert 88 <= zoho_retry.retry_after_seconds(format_datetime(future, usegmt=True)) <= 90
    assert zoho_retry.retry_after_seconds("bad") == 0
    assert zoho_retry.retry_after_seconds("-10") == 0


def test_validation_is_not_retried(monkeypatch):
    delays = []
    monkeypatch.setattr(zoho_retry.time, "sleep", delays.append)
    with pytest.raises(ValueError):
        zoho_retry.retry_read(lambda: (_ for _ in ()).throw(ValueError("invalid record")))
    assert not delays
