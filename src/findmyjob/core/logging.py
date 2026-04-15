from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from findmyjob.core.enums import LogRedactionMode

EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PHONE_RE = re.compile(r"\b(?:\+?\d[\d\s().-]{5,}\d)\b")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE)
ASSIGNED_SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password)\b\s*[:=]\s*([^\s,;]+)")
SECRET_SHAPE_RE = re.compile(r"\b(?:sk-[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AIza[0-9A-Za-z_-]{20,})\b")
ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Za-z0-9.#\-\' ]+\s(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|parkway|pkwy)\b(?:[^\n,]*(?:,\s*[A-Za-z .\'-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?)?",
    re.IGNORECASE,
)

_SECRET_KEYS = {"api_key", "apikey", "token", "secret", "password", "authorization"}
_CONTACT_KEYS = {"email", "phone", "contact", "address", "street", "city", "state", "postal_code", "zip", "zipcode", "linkedin"}
_DOCUMENT_KEYS = {"resume", "cover_letter", "resume_text", "cover_letter_text"}
_LITERAL_SAFE_KEYS = {"generated_at", "checked_at", "timestamp", "platform", "python", "version"}
_DEFAULT_REDACTION_MODE = LogRedactionMode.SAFE


def _coerce_mode(mode: LogRedactionMode | str | None) -> LogRedactionMode:
    if isinstance(mode, LogRedactionMode):
        return mode
    if isinstance(mode, str) and mode:
        return LogRedactionMode(mode)
    return _DEFAULT_REDACTION_MODE


def _looks_like_path(value: str) -> bool:
    cleaned = str(value or "").strip()
    if not cleaned:
        return False
    if "/" in cleaned or "\\" in cleaned:
        return True
    lowered = cleaned.lower()
    return lowered.endswith((".pdf", ".txt", ".html", ".png", ".jpg", ".jpeg", ".zip", ".json", ".yaml", ".yml"))


def _placeholder_for_key(key: str) -> str:
    if "email" in key:
        return "[redacted-email]"
    if "phone" in key:
        return "[redacted-phone]"
    if "address" in key or key in {"street", "city", "state", "postal_code", "zip", "zipcode"}:
        return "[redacted-address]"
    if key in _DOCUMENT_KEYS:
        return "[redacted-document]"
    if key in _SECRET_KEYS:
        return "[redacted-secret]"
    return "[redacted-contact]"


def _redact_phone_match(match: re.Match[str]) -> str:
    value = match.group(0)
    if ISO_DATE_RE.fullmatch(value):
        return value
    return "[redacted-phone]"


def redact_string(value: str, mode: LogRedactionMode | str | None = None) -> str:
    redaction_mode = _coerce_mode(mode)
    redacted = EMAIL_RE.sub("[redacted-email]", value)
    redacted = PHONE_RE.sub(_redact_phone_match, redacted)
    redacted = BEARER_RE.sub("Bearer [redacted-token]", redacted)
    redacted = ASSIGNED_SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted-secret]", redacted)
    redacted = SECRET_SHAPE_RE.sub("[redacted-secret]", redacted)
    redacted = ADDRESS_RE.sub("[redacted-address]", redacted)
    if redaction_mode == LogRedactionMode.STRICT and "linkedin.com/in/" in redacted.lower():
        redacted = re.sub(r"https?://(?:www\.)?linkedin\.com/in/[^\s]+", "[redacted-linkedin]", redacted, flags=re.IGNORECASE)
    return redacted


def redact_data(value: Any, *, mode: LogRedactionMode | str | None = None, key_path: tuple[str, ...] = ()) -> Any:
    redaction_mode = _coerce_mode(mode)
    if isinstance(value, str):
        last_key = key_path[-1].lower() if key_path else ""
        if last_key in _LITERAL_SAFE_KEYS or last_key.endswith(("_at", "_date", "_time")):
            return value
        redacted = redact_string(value, redaction_mode)
        if last_key in _SECRET_KEYS:
            return "[redacted-secret]"
        if last_key in _DOCUMENT_KEYS:
            return redacted if _looks_like_path(value) else "[redacted-document]"
        if last_key in _CONTACT_KEYS:
            if _looks_like_path(value):
                return redacted
            if redaction_mode == LogRedactionMode.STRICT:
                return _placeholder_for_key(last_key)
            return redacted if redacted != value else _placeholder_for_key(last_key)
        return redacted
    if isinstance(value, list):
        return [redact_data(item, mode=redaction_mode, key_path=key_path) for item in value]
    if isinstance(value, tuple):
        return [redact_data(item, mode=redaction_mode, key_path=key_path) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item, mode=redaction_mode, key_path=(*key_path, str(key))) for key, item in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, *, mode: LogRedactionMode | str | None = None) -> None:
        super().__init__()
        self.mode = _coerce_mode(mode)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_string(record.getMessage(), self.mode),
        }
        if record.exc_info:
            payload["exception"] = redact_string(self.formatException(record.exc_info), self.mode)
        return json.dumps(payload, default=str)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, *, mode: LogRedactionMode | str | None = None) -> None:
        super().__init__(fmt)
        self.mode = _coerce_mode(mode)

    def format(self, record: logging.LogRecord) -> str:
        return redact_string(super().format(record), self.mode)


def _reset_library_logger(name: str, *, level: int) -> None:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = True
    logger.setLevel(level)



def configure_logging(level: str = "INFO", structured: bool = False, redaction_mode: LogRedactionMode | str = LogRedactionMode.SAFE) -> None:
    global _DEFAULT_REDACTION_MODE
    _DEFAULT_REDACTION_MODE = _coerce_mode(redaction_mode)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stderr)
    if structured:
        handler.setFormatter(JsonFormatter(mode=_DEFAULT_REDACTION_MODE))
    else:
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", mode=_DEFAULT_REDACTION_MODE))
    root.addHandler(handler)

    # Keep library noise local and avoid stale handlers from tools that call fileConfig().
    _reset_library_logger("alembic", level=logging.WARNING)
    _reset_library_logger("alembic.runtime.migration", level=logging.NOTSET)
    _reset_library_logger("sqlalchemy", level=logging.WARNING)
    _reset_library_logger("sqlalchemy.engine", level=logging.NOTSET)
