from __future__ import annotations

from typing import Any

from findmyjob.db.models import JobPosting


def csv_values(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]



def enum_values(enum_type, raw: str) -> list[Any]:
    values: list[Any] = []
    for item in csv_values(raw):
        try:
            values.append(enum_type(item.lower()))
        except Exception:
            continue
    return values



def join_values(values: list[Any]) -> str:
    return ", ".join(getattr(value, "value", str(value)) for value in values if value is not None)



def int_value(raw: str) -> int | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None



def tri_bool(raw: str) -> bool | None:
    token = str(raw or "").strip().lower()
    if not token or token in {"any", "none", "null"}:
        return None
    if token in {"present", "true", "yes"}:
        return True
    if token in {"missing", "false", "no"}:
        return False
    return None



def render_lines(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line)



def job_location(job: JobPosting) -> str:
    parts = [job.city, job.region_code, job.country_code]
    structured = ", ".join(part for part in parts if part)
    return structured or job.location_raw or ""



def job_compensation(job: JobPosting) -> str:
    if job.compensation_min is None and job.compensation_max is None:
        return ""
    currency = job.compensation_currency or ""
    interval = job.compensation_interval or ""
    if job.compensation_min is not None and job.compensation_max is not None:
        amount = f"{job.compensation_min:,}-{job.compensation_max:,}"
    else:
        amount = f"{(job.compensation_min if job.compensation_min is not None else job.compensation_max):,}"
    if currency:
        amount = f"{currency} {amount}"
    if interval:
        amount = f"{amount}/{interval}"
    return amount
