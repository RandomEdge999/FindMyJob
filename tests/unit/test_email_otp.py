from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from findmyjob.core.email_otp import (
    _extract_greenhouse_application_receipt,
    _extract_greenhouse_security_code,
)


def _message(*, subject: str, body: str, sender: str = "Greenhouse <no-reply@us.greenhouse-mail.io>", recipient: str = "user@example.com", sent_at: datetime | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = (sent_at or datetime.now(timezone.utc)).strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(body)
    return msg


def test_extract_greenhouse_security_code_from_matching_message() -> None:
    msg = _message(
        subject="Security code for your application to Discord",
        body="Hi Jordan,\n\nCopy and paste this code into the security code field on your application:\n\nKyLB26gG\nAfter you enter the code, resubmit your application.\n",
        recipient="candidate@example.com",
    )

    code = _extract_greenhouse_security_code(
        msg,
        recipient="candidate@example.com",
        cutoff=datetime.now(timezone.utc) - timedelta(minutes=5),
        from_contains="greenhouse",
    )

    assert code == "KyLB26gG"


def test_extract_greenhouse_application_receipt_matches_company_and_role() -> None:
    msg = _message(
        subject="Thank you for applying to Discord!",
        body=(
            "Hey Jordan,\n\n"
            "Thanks so much for your interest in Discord! "
            "We successfully received your application for the Software Engineer, Safety Experience role "
            "and are pumped to look over it.\n"
        ),
        recipient="candidate@example.com",
    )

    receipt = _extract_greenhouse_application_receipt(
        msg,
        recipient="candidate@example.com",
        company="discord",
        role="softwareengineersafetyexperience",
        cutoff=datetime.now(timezone.utc) - timedelta(minutes=5),
        from_contains="greenhouse",
    )

    assert receipt is not None
    assert receipt.company == "Discord"
    assert receipt.role == "Software Engineer, Safety Experience"
    assert receipt.subject == "Thank you for applying to Discord!"


def test_extract_greenhouse_application_receipt_rejects_wrong_company() -> None:
    msg = _message(
        subject="Thank you for applying to Anthropic",
        body="We successfully received your application for the Data Scientist, Finance Forecasting position.",
        recipient="candidate@example.com",
    )

    receipt = _extract_greenhouse_application_receipt(
        msg,
        recipient="candidate@example.com",
        company="discord",
        role="",
        cutoff=datetime.now(timezone.utc) - timedelta(minutes=5),
        from_contains="greenhouse",
    )

    assert receipt is None
