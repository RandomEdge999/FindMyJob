"""Retry utilities with exponential backoff for HTTP and browser operations."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, TypeVar

import httpx

log = logging.getLogger("findmyjob.retry")

T = TypeVar("T")

# Transient HTTP errors that should be retried
RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def _retry_delay(attempt: int, backoff_base: float, backoff_max: float) -> float:
    return min(backoff_base ** attempt, backoff_max) * (0.5 + random.random() * 0.5)


class RetryableHTTPError(Exception):
    """Wraps an httpx.HTTPStatusError for a retryable status code."""

    def __init__(self, original: httpx.HTTPStatusError) -> None:
        self.original = original
        self.status_code = original.response.status_code
        super().__init__(str(original))


def is_retryable_http_error(exc: BaseException) -> bool:
    """Check if an exception represents a retryable HTTP error."""
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_HTTP_CODES
    if isinstance(exc, RetryableHTTPError):
        return True
    return False


async def http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
    retryable_codes: frozenset[int] = RETRYABLE_HTTP_CODES,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP GET with retry and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.get(url, **kwargs)
            status = getattr(response, "status_code", 200)
            if status in retryable_codes and attempt < max_retries:
                wait = _retry_delay(attempt, backoff_base, backoff_max)
                log.warning("[retry] HTTP GET %s returned %d (attempt %d/%d), retrying in %.1fs",
                            url, status, attempt, max_retries, wait)
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt < max_retries:
                wait = _retry_delay(attempt, backoff_base, backoff_max)
                log.warning("[retry] HTTP GET %s failed (attempt %d/%d): %s, retrying in %.1fs",
                            url, attempt, max_retries, exc, wait)
                await asyncio.sleep(wait)
            else:
                raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in retryable_codes and attempt < max_retries:
                last_error = exc
                wait = _retry_delay(attempt, backoff_base, backoff_max)
                log.warning("[retry] HTTP GET %s returned %d (attempt %d/%d), retrying in %.1fs",
                            url, exc.response.status_code, attempt, max_retries, wait)
                await asyncio.sleep(wait)
            else:
                raise
    raise last_error  # type: ignore[misc]


async def http_post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
    retryable_codes: frozenset[int] = RETRYABLE_HTTP_CODES,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP POST with retry and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.post(url, **kwargs)
            status = getattr(response, "status_code", 200)
            if status in retryable_codes and attempt < max_retries:
                wait = _retry_delay(attempt, backoff_base, backoff_max)
                log.warning("[retry] HTTP POST %s returned %d (attempt %d/%d), retrying in %.1fs",
                            url, status, attempt, max_retries, wait)
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt < max_retries:
                wait = _retry_delay(attempt, backoff_base, backoff_max)
                log.warning("[retry] HTTP POST %s failed (attempt %d/%d): %s, retrying in %.1fs",
                            url, attempt, max_retries, exc, wait)
                await asyncio.sleep(wait)
            else:
                raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in retryable_codes and attempt < max_retries:
                last_error = exc
                wait = _retry_delay(attempt, backoff_base, backoff_max)
                log.warning("[retry] HTTP POST %s returned %d (attempt %d/%d), retrying in %.1fs",
                            url, exc.response.status_code, attempt, max_retries, wait)
                await asyncio.sleep(wait)
            else:
                raise
    raise last_error  # type: ignore[misc]


def classify_error_retryability(exc: Exception) -> dict[str, Any]:
    """Classify whether an error is retryable and provide details.

    Returns:
        dict with keys: retryable (bool), error_type (str), message (str), category (str)
    """
    if isinstance(exc, httpx.ConnectError):
        return {
            "retryable": True,
            "error_type": "connection_error",
            "message": str(exc),
            "category": "network",
        }
    if isinstance(exc, httpx.TimeoutException):
        return {
            "retryable": True,
            "error_type": "timeout",
            "message": str(exc),
            "category": "network",
        }
    if isinstance(exc, httpx.HTTPStatusError):
        retryable = exc.response.status_code in RETRYABLE_HTTP_CODES
        return {
            "retryable": retryable,
            "error_type": f"http_{exc.response.status_code}",
            "message": str(exc),
            "category": "rate_limit" if exc.response.status_code == 429 else "server_error" if retryable else "client_error",
        }
    # Browser/Playwright errors
    exc_name = type(exc).__name__.lower()
    if "timeout" in exc_name:
        return {
            "retryable": True,
            "error_type": "browser_timeout",
            "message": str(exc),
            "category": "browser",
        }
    if "crash" in exc_name or "disconnect" in exc_name:
        return {
            "retryable": True,
            "error_type": "browser_crash",
            "message": str(exc),
            "category": "browser",
        }
    # Captcha, login wall, validation - not retryable
    msg_lower = str(exc).lower()
    if any(term in msg_lower for term in ("captcha", "login", "account wall")):
        return {
            "retryable": False,
            "error_type": "access_blocked",
            "message": str(exc),
            "category": "access",
        }
    return {
        "retryable": False,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "category": "unknown",
    }
