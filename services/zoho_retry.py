"""Bounded retries for read-only Zoho operations; writes require reconciliation."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import random
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError


T = TypeVar("T")
MAX_ATTEMPTS = 4
MAX_WAIT_SECONDS = 45.0
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def retry_after_seconds(value: str | None) -> float:
    """Accept both HTTP Retry-After forms without shortening the provider delay."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(value)))
    except (ValueError, OverflowError):
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return max(0.0, (instant - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def retry_read(operation: Callable[[], T]) -> T:
    """Retry transport/transient HTTP failures, including response-body reads.

    Long provider cooldowns stop the operation rather than retrying too early.
    Validation, permissions, malformed JSON and permanent errors are not retried.
    """
    waited = 0.0
    for attempt in range(MAX_ATTEMPTS):
        try:
            return operation()
        except (HTTPError, URLError, TimeoutError, ConnectionError, HTTPException) as exc:
            delay = 0.0
            if isinstance(exc, HTTPError):
                if exc.code not in TRANSIENT_STATUS:
                    raise
                delay = retry_after_seconds(exc.headers.get("Retry-After")) if exc.headers else 0.0
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = max(delay, 2 ** attempt + random.uniform(0, 0.25 * 2 ** attempt))
            if waited + delay > MAX_WAIT_SECONDS:
                raise
            if isinstance(exc, HTTPError):
                exc.close()
            time.sleep(delay)
            waited += delay
