from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
import imaplib
import os
import re
import time


_GREENHOUSE_CODE_PATTERN = re.compile(
    r"security code field on your application:\s*([A-Za-z0-9]{8})",
    re.IGNORECASE,
)
_GREENHOUSE_BODY_MARKERS = (
    "security code field on your application",
    "after you enter the code, resubmit your application",
)
_GREENHOUSE_RECEIPT_BODY_MARKERS = (
    "successfully received your application",
    "thank you for applying",
)
_GREENHOUSE_RECEIPT_SUBJECT_PATTERN = re.compile(
    r"thank you for applying to\s+(?P<company>.+?)[!.\s]*$",
    re.IGNORECASE,
)
_GREENHOUSE_RECEIPT_ROLE_PATTERNS = (
    re.compile(r"successfully received your application for the\s+(?P<role>.+?)\s+(?:role|position)\b", re.IGNORECASE),
    re.compile(r"submit an application for the\s+(?P<role>.+?)\s+(?:role|position)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class EmailOtpSettings:
    host: str
    port: int
    username: str
    password: str
    folder: str = "INBOX"
    from_contains: str = "greenhouse"
    timeout_seconds: int = 90
    poll_interval_seconds: float = 3.0
    max_messages: int = 20


@dataclass(frozen=True)
class GreenhouseApplicationReceipt:
    subject: str
    sender: str
    recipient: str
    company: str | None = None
    role: str | None = None
    message_datetime: datetime | None = None
    body_snippet: str | None = None


def load_email_otp_settings() -> EmailOtpSettings | None:
    enabled = str(os.environ.get("FMJ_EMAIL_OTP_ENABLED") or "").strip().casefold()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    host = str(os.environ.get("FMJ_EMAIL_OTP_HOST") or "").strip()
    username = str(os.environ.get("FMJ_EMAIL_OTP_USERNAME") or "").strip()
    password_env = str(os.environ.get("FMJ_EMAIL_OTP_PASSWORD_ENV") or "").strip()
    password = str(os.environ.get(password_env) or "").strip() if password_env else str(os.environ.get("FMJ_EMAIL_OTP_PASSWORD") or "").strip()
    if "gmail" in host.casefold() and password:
        password = re.sub(r"\s+", "", password)
    if not host or not username or not password:
        return None
    try:
        port = int(os.environ.get("FMJ_EMAIL_OTP_PORT") or 993)
    except (TypeError, ValueError):
        port = 993
    try:
        timeout_seconds = max(5, int(os.environ.get("FMJ_EMAIL_OTP_TIMEOUT_SECONDS") or 90))
    except (TypeError, ValueError):
        timeout_seconds = 90
    try:
        poll_interval_seconds = max(1.0, float(os.environ.get("FMJ_EMAIL_OTP_POLL_SECONDS") or 3))
    except (TypeError, ValueError):
        poll_interval_seconds = 3.0
    try:
        max_messages = max(5, int(os.environ.get("FMJ_EMAIL_OTP_MAX_MESSAGES") or 20))
    except (TypeError, ValueError):
        max_messages = 20
    return EmailOtpSettings(
        host=host,
        port=port,
        username=username,
        password=password,
        folder=str(os.environ.get("FMJ_EMAIL_OTP_FOLDER") or "INBOX").strip() or "INBOX",
        from_contains=str(os.environ.get("FMJ_EMAIL_OTP_FROM_CONTAINS") or "greenhouse").strip() or "greenhouse",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        max_messages=max_messages,
    )


def fetch_greenhouse_security_code(
    *,
    recipient: str | None = None,
    issued_after: datetime | None = None,
) -> str | None:
    settings = load_email_otp_settings()
    if settings is None:
        return None
    recipient_normalized = str(recipient or "").strip().casefold()
    cutoff = issued_after or (datetime.now(timezone.utc) - timedelta(minutes=5))
    deadline = time.monotonic() + settings.timeout_seconds
    while True:
        code = _poll_once(settings, recipient=recipient_normalized, cutoff=cutoff)
        if code:
            return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(settings.poll_interval_seconds)


def fetch_greenhouse_application_receipt(
    *,
    company: str | None = None,
    role: str | None = None,
    recipient: str | None = None,
    issued_after: datetime | None = None,
    timeout_seconds: int | None = None,
) -> GreenhouseApplicationReceipt | None:
    settings = load_email_otp_settings()
    if settings is None:
        return None
    recipient_normalized = str(recipient or "").strip().casefold()
    company_normalized = _normalize_mail_token(company)
    role_normalized = _normalize_mail_token(role)
    cutoff = issued_after or (datetime.now(timezone.utc) - timedelta(minutes=5))
    timeout = max(5, int(timeout_seconds or min(settings.timeout_seconds, 45)))
    deadline = time.monotonic() + timeout
    while True:
        receipt = _poll_for_receipt_once(
            settings,
            recipient=recipient_normalized,
            company=company_normalized,
            role=role_normalized,
            cutoff=cutoff,
        )
        if receipt is not None:
            return receipt
        if time.monotonic() >= deadline:
            return None
        time.sleep(settings.poll_interval_seconds)


def _poll_once(settings: EmailOtpSettings, *, recipient: str, cutoff: datetime) -> str | None:
    with imaplib.IMAP4_SSL(settings.host, settings.port) as client:
        client.login(settings.username, settings.password)
        status, _ = client.select(settings.folder, readonly=True)
        if status != "OK":
            return None
        status, data = client.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()[-settings.max_messages :]
        for uid in reversed(uids):
            status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw_message = next((part[1] for part in payload if isinstance(part, tuple) and len(part) > 1), None)
            if not raw_message:
                continue
            message = message_from_bytes(raw_message)
            code = _extract_greenhouse_security_code(message, recipient=recipient, cutoff=cutoff, from_contains=settings.from_contains)
            if code:
                return code
    return None


def _poll_for_receipt_once(
    settings: EmailOtpSettings,
    *,
    recipient: str,
    company: str,
    role: str,
    cutoff: datetime,
) -> GreenhouseApplicationReceipt | None:
    with imaplib.IMAP4_SSL(settings.host, settings.port) as client:
        client.login(settings.username, settings.password)
        status, _ = client.select(settings.folder, readonly=True)
        if status != "OK":
            return None
        status, data = client.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()[-settings.max_messages :]
        for uid in reversed(uids):
            status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw_message = next((part[1] for part in payload if isinstance(part, tuple) and len(part) > 1), None)
            if not raw_message:
                continue
            message = message_from_bytes(raw_message)
            receipt = _extract_greenhouse_application_receipt(
                message,
                recipient=recipient,
                company=company,
                role=role,
                cutoff=cutoff,
                from_contains=settings.from_contains,
            )
            if receipt is not None:
                return receipt
    return None


def _extract_greenhouse_security_code(
    message: Message,
    *,
    recipient: str,
    cutoff: datetime,
    from_contains: str,
) -> str | None:
    sender_blob = " ".join(addr for _, addr in getaddresses(message.get_all("from", []))).casefold()
    if from_contains and from_contains.casefold() not in sender_blob:
        return None
    if recipient:
        recipients = " ".join(addr for _, addr in getaddresses(message.get_all("to", []) + message.get_all("cc", []))).casefold()
        if recipients and recipient not in recipients:
            return None
    message_dt = _message_datetime(message)
    if message_dt is not None and message_dt < cutoff:
        return None
    body = _message_body(message)
    lowered = body.casefold()
    if not all(marker in lowered for marker in _GREENHOUSE_BODY_MARKERS):
        return None
    match = _GREENHOUSE_CODE_PATTERN.search(body)
    if not match:
        return None
    return match.group(1)


def _extract_greenhouse_application_receipt(
    message: Message,
    *,
    recipient: str,
    company: str,
    role: str,
    cutoff: datetime,
    from_contains: str,
) -> GreenhouseApplicationReceipt | None:
    sender_text = _decoded_header_text(message, "from")
    sender_blob = " ".join(addr for _, addr in getaddresses(message.get_all("from", []))).casefold()
    if from_contains and from_contains.casefold() not in sender_blob and from_contains.casefold() not in sender_text.casefold():
        return None
    recipient_text = " ".join(addr for _, addr in getaddresses(message.get_all("to", []) + message.get_all("cc", [])))
    if recipient and recipient_text and recipient not in recipient_text.casefold():
        return None
    message_dt = _message_datetime(message)
    if message_dt is not None and message_dt < cutoff:
        return None
    subject = _decoded_header_text(message, "subject")
    body = _message_body(message)
    lowered_body = body.casefold()
    if not (
        _GREENHOUSE_RECEIPT_SUBJECT_PATTERN.search(subject)
        or all(marker in lowered_body for marker in _GREENHOUSE_RECEIPT_BODY_MARKERS)
    ):
        return None
    parsed_company = _extract_company_from_receipt_subject(subject)
    if company:
        company_candidates = [parsed_company, subject, body]
        if not any(company and company in _normalize_mail_token(candidate) for candidate in company_candidates if str(candidate or "").strip()):
            return None
    parsed_role = _extract_role_from_receipt_body(body)
    if role:
        role_candidates = [parsed_role, subject, body]
        if not any(role and role in _normalize_mail_token(candidate) for candidate in role_candidates if str(candidate or "").strip()):
            return None
    return GreenhouseApplicationReceipt(
        subject=subject,
        sender=sender_text,
        recipient=recipient_text,
        company=parsed_company,
        role=parsed_role,
        message_datetime=message_dt,
        body_snippet=_body_snippet(body),
    )


def _message_datetime(message: Message) -> datetime | None:
    raw_date = str(message.get("date") or "").strip()
    if not raw_date:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decoded_header_text(message: Message, name: str) -> str:
    raw = str(message.get(name) or "").strip()
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw


def _message_body(message: Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = str(part.get_content_type() or "").casefold()
            if content_type not in {"text/plain", "text/html"}:
                continue
            if str(part.get("content-disposition") or "").casefold().startswith("attachment"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="ignore")
            except Exception:
                text = payload.decode("utf-8", errors="ignore")
            parts.append(_html_to_text(text) if content_type == "text/html" else text)
    else:
        payload = message.get_payload(decode=True) or b""
        charset = message.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="ignore")
        except Exception:
            text = payload.decode("utf-8", errors="ignore")
        parts.append(_html_to_text(text) if str(message.get_content_type() or "").casefold() == "text/html" else text)
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _html_to_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_company_from_receipt_subject(subject: str) -> str | None:
    match = _GREENHOUSE_RECEIPT_SUBJECT_PATTERN.search(str(subject or "").strip())
    if not match:
        return None
    company = str(match.group("company") or "").strip(" !.")
    return company or None


def _extract_role_from_receipt_body(body: str) -> str | None:
    text = str(body or "").strip()
    for pattern in _GREENHOUSE_RECEIPT_ROLE_PATTERNS:
        match = pattern.search(text)
        if match:
            role = str(match.group("role") or "").strip(" .!")
            if role:
                return role
    return None


def _normalize_mail_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def _body_snippet(body: str, *, limit: int = 240) -> str | None:
    text = " ".join(str(body or "").split()).strip()
    if not text:
        return None
    return text[:limit]
