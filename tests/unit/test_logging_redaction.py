from findmyjob.core.logging import redact_data, redact_string


def test_redact_string_covers_email_phone_secret_and_address() -> None:
    value = (
        "Reach me at jane@example.com or +1 (555) 010-0199. "
        "Bearer abcdefghijklmnopqrstuvwx token=sk-testsecretvalue123456 "
        "123 Main Street, Austin, TX 78701"
    )

    redacted = redact_string(value)

    assert "[redacted-email]" in redacted
    assert "[redacted-phone]" in redacted
    assert "[redacted-secret]" in redacted
    assert "[redacted-address]" in redacted


def test_redact_string_keeps_iso_dates_visible() -> None:
    assert redact_string('2026-05-01') == '2026-05-01'


def test_redact_data_keeps_paths_visible_but_redacts_contact_fields() -> None:
    payload = {
        "email": "jane@example.com",
        "phone": "555-0100",
        "resume": "C:/workspace/artifacts/resume.pdf",
        "resume_text": "Worked at Acme for five years.",
        "token": "super-secret-token",
        "notes": "Call me at 555-2222",
    }

    redacted = redact_data(payload)

    assert redacted["email"] == "[redacted-email]"
    assert redacted["phone"] == "[redacted-phone]"
    assert redacted["resume"] == "C:/workspace/artifacts/resume.pdf"
    assert redacted["resume_text"] == "[redacted-document]"
    assert redacted["token"] == "[redacted-secret]"
    assert "[redacted-phone]" in redacted["notes"]
