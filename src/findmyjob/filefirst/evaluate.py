from __future__ import annotations

import hashlib
import logging
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from findmyjob.core.enums import ModelRole
from findmyjob.filefirst.modes import ModeRunner
from findmyjob.filefirst.models import ApplicationEntry, EvaluationResult, FileFact, InboxJob
from findmyjob.filefirst.prompt_budget import compact_fact_payload, compact_profile_payload, json_chars, trim_text
from findmyjob.filefirst.screening import screen_job, screening_payload
from findmyjob.filefirst.text_utils import strip_html_tags
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.normalizer import slugify


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            cleaned = " ".join(data.split())
            if cleaned:
                self.parts.append(cleaned)


def _grade_for_score(score: float) -> str:
    if score >= 4.5:
        return "A"
    if score >= 4.0:
        return "B"
    if score >= 3.0:
        return "C"
    if score >= 2.0:
        return "D"
    if score >= 1.0:
        return "E"
    return "F"


def _extract_text_from_html(html: str) -> tuple[str, str | None]:
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    heading_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    title = None
    for match in (heading_match, title_match):
        if match:
            title = unescape(re.sub(r"<[^>]+>", " ", match.group(1))).strip()
            if title:
                break
    body = "\n".join(extractor.parts)
    return body, title


_EVAL_PROMPT_CHAR_BUDGET = 10_000
_EVAL_CONTEXT_TIERS = (
    {
        "job_chars": 3200,
        "cv_chars": 1600,
        "work_limit": 5,
        "project_limit": 3,
        "skill_limit": 8,
        "detail": "normal",
    },
    {
        "job_chars": 2400,
        "cv_chars": 1000,
        "work_limit": 4,
        "project_limit": 2,
        "skill_limit": 6,
        "detail": "tight",
    },
    {
        "job_chars": 1800,
        "cv_chars": 0,
        "work_limit": 3,
        "project_limit": 2,
        "skill_limit": 4,
        "detail": "tight",
    },
)


def _fact_text(fact: FileFact) -> str:
    return " ".join(str(value) for value in fact.payload.values() if str(value or "").strip())


def _job_terms(job: InboxJob) -> set[str]:
    haystack = re.sub(r"[^a-z0-9]+", " ", f"{job.title} {strip_html_tags(job.description)}".casefold())
    return {token for token in haystack.split() if len(token) > 2}


def _fact_score(fact: FileFact, terms: set[str]) -> int:
    if not terms:
        return 0
    haystack = re.sub(r"[^a-z0-9]+", " ", _fact_text(fact).casefold())
    return len({token for token in haystack.split() if len(token) > 2} & terms)


def _compact_job_for_llm(job: InboxJob) -> InboxJob:
    return job.model_copy(update={"description": strip_html_tags(job.description)})


def _ranked_eval_facts(facts: list[FileFact], job: InboxJob) -> tuple[list[FileFact], list[FileFact], list[FileFact], list[FileFact]]:
    terms = _job_terms(job)
    essentials = [fact for fact in facts if fact.kind in {"contact", "authorization", "location", "education"} and not fact.disallowed]
    work = sorted(
        [fact for fact in facts if fact.kind == "work" and not fact.disallowed],
        key=lambda item: (_fact_score(item, terms), item.fact_id),
        reverse=True,
    )
    projects = sorted(
        [fact for fact in facts if fact.kind == "project" and not fact.disallowed],
        key=lambda item: (_fact_score(item, terms), item.fact_id),
        reverse=True,
    )
    skills = sorted(
        [fact for fact in facts if fact.kind == "skill" and not fact.disallowed],
        key=lambda item: (_fact_score(item, terms), item.fact_id),
        reverse=True,
    )
    return essentials, work, projects, skills


def _facts_for_eval_context(
    facts: list[FileFact],
    job: InboxJob,
    *,
    work_limit: int,
    project_limit: int,
    skill_limit: int,
) -> list[FileFact]:
    essentials, work, projects, skills = _ranked_eval_facts(facts, job)
    keep: list[FileFact] = []
    seen: set[str] = set()

    def add(items: list[FileFact]) -> None:
        for item in items:
            if item.fact_id in seen:
                continue
            seen.add(item.fact_id)
            keep.append(item)

    add(essentials)
    add(work[:work_limit])
    add(projects[:project_limit])
    add(skills[:skill_limit])
    return keep


def _profile_for_llm(profile) -> dict[str, Any]:
    return compact_profile_payload(profile)


def _fact_for_llm(fact: FileFact, *, detail: str) -> dict[str, Any]:
    return compact_fact_payload(fact, detail=detail)


def _ad_hoc_job_from_text(target: str, body: str, *, title: str | None = None, url: str | None = None) -> InboxJob:
    host = urlparse(url).hostname if url else None
    company = ((host or "local-file").split(".")[0] or "candidate-target").replace("-", " ").title()
    role = title or Path(target).stem.replace("_", " ").replace("-", " ").title() or "Unlabeled Role"
    source_kind = "manual"
    source_job_id = hashlib.sha256((url or target).encode("utf-8")).hexdigest()[:12]
    job_id = hashlib.sha256(f"{source_kind}|{company.casefold()}|{source_job_id}".encode("utf-8")).hexdigest()
    return InboxJob(
        job_id=job_id,
        company=company,
        company_key=slugify(company),
        title=role,
        source="manual",
        source_kind=source_kind,
        source_job_id=source_job_id,
        url=url or target,
        apply_url=url,
        location=None,
        description=body,
        board_family="unknown",
        automation_tier="unsupported_high_friction",
        job_identity_key=job_id,
        duplicate_cluster_key=hashlib.sha256(f"{company.casefold()}|{role.casefold()}".encode("utf-8")).hexdigest(),
    )


def _resolve_target(ws: FileWorkspace, target: str) -> InboxJob:
    job = ws.load_job(target)
    if job is not None:
        return job
    application = ws.find_application(target)
    if application is not None:
        job = ws.load_job(application.job_id)
        if job is not None:
            return job
    candidate_path = Path(target)
    if candidate_path.exists() and candidate_path.is_file():
        body = candidate_path.read_text(encoding="utf-8")
        job = _ad_hoc_job_from_text(str(candidate_path), body, title=candidate_path.stem)
        ws.save_job(job)
        return job
    if re.match(r"https?://", target, re.IGNORECASE):
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(target)
            response.raise_for_status()
            body, title = _extract_text_from_html(response.text)
        job = _ad_hoc_job_from_text(target, body, title=title, url=target)
        ws.save_job(job)
        return job
    raise ValueError(f"Unknown evaluation target: {target}")


def _default_report(job: InboxJob, evaluation: EvaluationResult) -> str:
    lines = [
        f"# Evaluation: {evaluation.company} - {evaluation.role}",
        "",
        f"**Archetype:** {evaluation.archetype}",
        f"**Score:** {evaluation.score:.2f}/5 ({evaluation.grade})",
        f"**Source:** {evaluation.source}",
        f"**URL:** {evaluation.url}",
        "",
        "## Summary",
        evaluation.summary or "No summary generated.",
        "",
        "## Fit Reasons",
    ]
    lines.extend([f"- {item}" for item in evaluation.fit_reasons] or ["- No strong fit reasons generated."])
    lines.append("")
    lines.append("## Gaps")
    lines.extend([f"- {item}" for item in evaluation.gaps] or ["- No explicit gaps generated."])
    lines.append("")
    lines.append("## Keywords")
    lines.append(", ".join(evaluation.keywords) if evaluation.keywords else "No keywords generated.")
    lines.append("")
    lines.append("## Resume Strategy")
    if evaluation.resume_headline:
        lines.append(f"- Headline: {evaluation.resume_headline}")
    lines.extend([f"- {item}" for item in evaluation.resume_summary_lines] or ["- No resume notes generated."])
    return "\n".join(lines).rstrip() + "\n"


def _normalize_payload(job: InboxJob, payload: dict[str, Any]) -> EvaluationResult:
    score = float(payload.get("score", 0.0) or 0.0)
    score = max(0.0, min(5.0, score))
    grade = _grade_for_score(score)
    evaluation = EvaluationResult(
        job_id=job.job_id,
        company=str(payload.get("company") or job.company),
        role=str(payload.get("role") or job.title),
        source=job.source,
        url=job.url,
        archetype=str(payload.get("archetype") or "Generalist AI Engineer"),
        score=score,
        grade=grade,
        summary=str(payload.get("summary") or ""),
        keywords=[str(item).strip() for item in list(payload.get("keywords") or []) if str(item).strip()],
        fit_reasons=[str(item).strip() for item in list(payload.get("fit_reasons") or []) if str(item).strip()],
        gaps=[str(item).strip() for item in list(payload.get("gaps") or []) if str(item).strip()],
        report_markdown=str(payload.get("report_markdown") or ""),
        resume_headline=str(payload.get("resume_headline") or "").strip() or None,
        resume_summary_lines=[str(item).strip() for item in list(payload.get("resume_summary_lines") or []) if str(item).strip()],
        selected_work_fact_ids=[str(item).strip() for item in list(payload.get("selected_work_fact_ids") or []) if str(item).strip()],
        selected_project_fact_ids=[str(item).strip() for item in list(payload.get("selected_project_fact_ids") or []) if str(item).strip()],
        selected_skill_fact_ids=[str(item).strip() for item in list(payload.get("selected_skill_fact_ids") or []) if str(item).strip()],
        custom_bullets=[str(item).strip() for item in list(payload.get("custom_bullets") or []) if str(item).strip()],
        cover_letter_paragraphs=[str(item).strip() for item in list(payload.get("cover_letter_paragraphs") or []) if str(item).strip()],
    )
    if not evaluation.report_markdown.strip():
        evaluation.report_markdown = _default_report(job, evaluation)
    return evaluation


def _eval_context(ws: FileWorkspace, job: InboxJob) -> dict[str, Any]:
    profile_model = ws.load_profile()
    all_facts = ws.load_facts()
    cv_text = ws.load_cv()
    last_context: dict[str, Any] | None = None

    for tier in _EVAL_CONTEXT_TIERS:
        job_context = _compact_job_for_llm(job)
        job_context = job_context.model_copy(update={"description": trim_text(job_context.description, limit=int(tier["job_chars"]))})
        fact_context = _facts_for_eval_context(
            all_facts,
            job_context,
            work_limit=int(tier["work_limit"]),
            project_limit=int(tier["project_limit"]),
            skill_limit=int(tier["skill_limit"]),
        )
        context = {
            "profile": _profile_for_llm(profile_model),
            "facts": [_fact_for_llm(fact, detail=str(tier["detail"])) for fact in fact_context],
            "cv_markdown": trim_text(cv_text, limit=int(tier["cv_chars"])),
            "job": job_context.model_dump(mode="json"),
        }
        last_context = context
        if json_chars(context) <= _EVAL_PROMPT_CHAR_BUDGET:
            return context

    assert last_context is not None
    return last_context


def evaluate_target(workspace: Path | FileWorkspace, target: str) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    job = _resolve_target(ws, target)
    context = _eval_context(ws, job)
    runner = ModeRunner(ws)
    payload = runner.run_json("eval", context)
    model_profile = runner.last_profile_name
    model_role = runner.last_role or ModelRole.CLASSIFIER
    evaluation = _normalize_payload(job, payload)
    model_metadata: list[str] = []
    if model_profile:
        model_metadata.append(f"Model profile: {model_profile}")
    if model_role:
        model_metadata.append(f"Model role: {model_role.value}")
    if model_metadata:
        metadata_block = "\n".join(model_metadata)
        if metadata_block not in (evaluation.summary or ""):
            evaluation.summary = f"{evaluation.summary}\n\n{metadata_block}".strip()
    ws.save_job(job)
    ws.save_evaluation(evaluation)

    existing = ws.find_application(job.job_id)
    display_id = existing.id if existing is not None else ws.next_application_id()
    report_path = ws.report_path_for(display_id, evaluation.company)
    report_path.write_text(evaluation.report_markdown.rstrip() + "\n", encoding="utf-8")

    entry = ApplicationEntry(
        id=display_id,
        job_id=job.job_id,
        date=evaluation.evaluated_at[:10],
        company=evaluation.company,
        role=evaluation.role,
        score=evaluation.score,
        grade=evaluation.grade,
        status="Evaluated",
        pdf=bool(existing.pdf) if existing is not None else False,
        report=ws.relative_path(report_path),
        url=evaluation.url,
        source=evaluation.source,
    )
    ws.upsert_application(entry)
    ws.update_inbox_state(job.job_id, "evaluated")
    return {
        "application_id": display_id,
        "job_id": job.job_id,
        "company": evaluation.company,
        "role": evaluation.role,
        "score": evaluation.score,
        "grade": evaluation.grade,
        "report_path": ws.relative_path(report_path),
        "model_profile": model_profile,
        "model_role": model_role.value,
    }


def run_pipeline(workspace: Path | FileWorkspace, *, limit: int | None = None, generate_pdf: bool = True) -> dict[str, Any]:
    log = logging.getLogger("findmyjob.pipeline")
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    # Only process jobs that are still pending (not already evaluated)
    # Jobs with workflow_state="evaluated" have already been fully processed (screened + evaluated + PDF'd)
    pending = [job for job in ws.load_inbox() if job.workflow_state == "pending"]
    if limit is not None:
        pending = pending[:limit]
    screened: list[dict[str, Any]] = []
    screened_out: list[dict[str, Any]] = []
    evaluated: list[dict[str, Any]] = []
    pdfs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    from findmyjob.filefirst.render import build_pdf_for_target

    for job in pending:
        try:
            screened_job, screening = screen_job(ws, job.job_id)
        except Exception as exc:
            log.warning("Screening failed for %s: %s", job.job_id, exc)
            errors.append({"job_id": job.job_id, "stage": "screening", "error": str(exc)})
            continue
        payload = {
            "job_id": screened_job.job_id,
            "company": screened_job.company,
            "title": screened_job.title,
            "workflow_state": screened_job.workflow_state,
            "screening": screening_payload(screened_job),
        }
        screened.append(payload)
        if not screening.approved:
            screened_out.append(payload)
            continue
        try:
            result = evaluate_target(ws, screened_job.job_id)
            evaluated.append(result)
        except Exception as exc:
            log.warning("Evaluation failed for %s: %s", screened_job.job_id, exc)
            errors.append({"job_id": screened_job.job_id, "stage": "evaluation", "error": str(exc)})
            continue
        if generate_pdf:
            try:
                pdfs.append(build_pdf_for_target(ws, screened_job.job_id))
            except Exception as exc:
                log.warning("PDF generation failed for %s: %s", screened_job.job_id, exc)
                errors.append({"job_id": screened_job.job_id, "stage": "pdf", "error": str(exc)})
    return {
        "processed": len(pending),
        "screened": screened,
        "screened_out": screened_out,
        "evaluated": evaluated,
        "pdfs": pdfs,
        "errors": errors,
    }
