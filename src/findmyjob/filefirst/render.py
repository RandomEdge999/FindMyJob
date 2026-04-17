from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from findmyjob.core.async_compat import run_async
from findmyjob.core.config import AppConfig
from findmyjob.filefirst.chatgpt_drafting import ChatGPTDraftingService
from findmyjob.filefirst.models import FileFact, ResumePlan
from findmyjob.filefirst.text_utils import strip_html_tags
from findmyjob.filefirst.workspace import FileWorkspace


_MAX_PDF_CV_CONTEXT_CHARS = 5000
_MAX_PDF_JOB_DESCRIPTION_CHARS = 7000
_MAX_PDF_SKILL_FACTS = 24
_COVER_LETTER_PLACEHOLDER_RE = re.compile(
    r"(?i)(\bTODO\b|\[[^\]]*placeholder[^\]]*\]|\{[^}]+\}|(?:^|[\s(])(?:N|X)(?=$|[\s).,;:!?]))"
)

_TEXT_SANITIZER = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        " ": " ",
    }
)

_DEFAULT_PDF_RENDERER = "typst"
_HTML_TO_PDF_RENDERER = "html_to_pdf"
_CHATGPT_DOWNLOAD_RENDERER = "chatgpt_download"


def _clean_generated_text(value: str | None) -> str:
    text = str(value or "").replace("...[truncated]", "").translate(_TEXT_SANITIZER)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return text.strip()


def _display_company_name(value: str | None) -> str:
    text = _clean_generated_text(value)
    if not text:
        return "the company"
    if text == text.casefold():
        return " ".join(part.capitalize() for part in text.split())
    return text


def _normalize_bullets(items: list[str]) -> list[str]:
    return [cleaned for cleaned in (_clean_generated_text(item) for item in items) if cleaned]


def _contains_cover_letter_placeholder(value: str | None) -> bool:
    return bool(_COVER_LETTER_PLACEHOLDER_RE.search(str(value or "")))


def _cover_letter_paragraphs(job, evaluation, paragraphs: list[str]) -> list[str]:
    cleaned = []
    for paragraph in paragraphs:
        sanitized = _clean_generated_text(strip_html_tags(paragraph)).replace("...[truncated]", "").strip()
        if not sanitized or _contains_cover_letter_placeholder(sanitized):
            continue
        cleaned.append(sanitized)
    return cleaned[:3]


def _trim_text(value: str | None, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return (clipped or text[:limit].strip()) + "\n...[truncated]"


def _compact_job_for_llm(job):
    sanitized_description = strip_html_tags(job.description)
    return job.model_copy(update={"description": _trim_text(sanitized_description, limit=_MAX_PDF_JOB_DESCRIPTION_CHARS)})


def _facts_for_pdf_context(facts: list[FileFact], job, evaluation) -> list[FileFact]:
    selected_ids = list(evaluation.selected_work_fact_ids) + list(evaluation.selected_project_fact_ids) + list(evaluation.selected_skill_fact_ids)
    by_id = {fact.fact_id: fact for fact in facts}
    keep: list[FileFact] = []
    seen: set[str] = set()

    def add(items: list[FileFact]) -> None:
        for item in items:
            if item.fact_id in seen:
                continue
            seen.add(item.fact_id)
            keep.append(item)

    add([by_id[fact_id] for fact_id in selected_ids if fact_id in by_id])
    add([fact for fact in facts if fact.kind in {"contact", "authorization", "location", "preference", "personal", "education", "work", "project"}])
    ranked_skills = sorted([fact for fact in facts if fact.kind == "skill"], key=lambda item: (_score_fact(item, _terms(job, evaluation)), item.fact_id), reverse=True)
    add(ranked_skills[:_MAX_PDF_SKILL_FACTS])
    return keep


def _profile_for_llm(profile) -> dict[str, Any]:
    candidate = profile.candidate.model_dump(mode="json")
    targets = profile.targets.model_dump(mode="json")
    return {
        "candidate": {key: value for key, value in candidate.items() if value not in (None, "", [], {})},
        "targets": {key: value for key, value in targets.items() if value not in (None, "", [], {})},
    }


def _fact_for_llm(fact: FileFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "kind": fact.kind,
        "payload": fact.payload,
    }


def _fact_text(fact: FileFact) -> str:
    return " ".join(str(value) for value in fact.payload.values() if str(value or "").strip())


def _score_fact(fact: FileFact, terms: set[str]) -> int:
    haystack = re.sub(r"[^a-z0-9]+", " ", _fact_text(fact).casefold())
    payload_terms = {token for token in haystack.split() if len(token) > 2}
    return len(payload_terms & terms)


def _terms(job, evaluation) -> set[str]:
    raw = [job.title, strip_html_tags(job.description), *evaluation.keywords]
    text = re.sub(r"[^a-z0-9]+", " ", " ".join(str(item or "") for item in raw).casefold())
    return {token for token in text.split() if len(token) > 2}


def _contact_fact(facts: list[FileFact]) -> FileFact:
    for fact in facts:
        if fact.kind == "contact" and not fact.disallowed:
            return fact
    raise ValueError("Missing contact fact for PDF generation.")


def _select_facts(facts: list[FileFact], kind: str, selected_ids: list[str], terms: set[str], limit: int) -> list[FileFact]:
    allowed = [fact for fact in facts if fact.kind == kind and fact.allowed_for_generation and not fact.disallowed]
    by_id = {fact.fact_id: fact for fact in allowed}
    selected = [by_id[fact_id] for fact_id in selected_ids if fact_id in by_id]
    if selected:
        return selected[:limit]
    ranked = sorted(allowed, key=lambda item: (_score_fact(item, terms), item.fact_id), reverse=True)
    return ranked[:limit]


def _safe(value: str | None) -> str:
    return html.escape(str(value or ""))


def _bullet_list(items: list[str]) -> str:
    rows = [f"<li>{_safe(item)}</li>" for item in items if str(item or "").strip()]
    return "\n".join(rows)


def _render_resume_html(profile, contact: FileFact, work: list[FileFact], projects: list[FileFact], skills: list[FileFact], evaluation, plan: ResumePlan) -> str:
    contact_payload = contact.payload
    summary_lines = _normalize_bullets(plan.summary_lines or evaluation.resume_summary_lines or ([evaluation.summary] if evaluation.summary else []))
    skill_names = [str(fact.payload.get("name") or fact.payload.get("skill") or _fact_text(fact)).strip() for fact in skills]
    work_html: list[str] = []
    for fact in work:
        payload = fact.payload
        bullets = list(payload.get("bullets") or [])
        summary = str(payload.get("summary") or payload.get("description") or "").strip()
        details = [summary, *bullets[:4]]
        work_html.append(
            "<section class='card'>"
            f"<h3>{_safe(str(payload.get('title') or 'Experience'))}</h3>"
            f"<p class='meta'>{_safe(str(payload.get('company') or ''))}</p>"
            f"<ul>{_bullet_list([item for item in details if item])}</ul>"
            "</section>"
        )
    project_html: list[str] = []
    for fact in projects:
        payload = fact.payload
        details = [str(payload.get("summary") or payload.get("description") or "").strip()]
        project_html.append(
            "<section class='card'>"
            f"<h3>{_safe(str(payload.get('name') or 'Project'))}</h3>"
            f"<ul>{_bullet_list([item for item in details if item])}</ul>"
            "</section>"
        )
    custom_bullets = _normalize_bullets(plan.custom_bullets or evaluation.custom_bullets)
    headline = _clean_generated_text(plan.headline or evaluation.resume_headline or profile.candidate.summary or evaluation.role)
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <title>{_safe(contact_payload.get('name'))} - {_safe(evaluation.company)}</title>
  <style>
    :root {{ --ink:#111827; --muted:#4b5563; --line:#d1d5db; --accent:#0f766e; --panel:#f8fafc; }}
    body {{ font-family: 'Segoe UI', Tahoma, sans-serif; color: var(--ink); margin: 0; padding: 32px; background: white; }}
    header {{ border-bottom: 3px solid var(--accent); padding-bottom: 18px; margin-bottom: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 30px; }}
    h2 {{ margin: 22px 0 10px; font-size: 16px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent); }}
    h3 {{ margin: 0 0 4px; font-size: 16px; }}
    .meta, .subtle {{ color: var(--muted); }}
    .lead {{ font-size: 15px; line-height: 1.55; margin: 10px 0 0; }}
    .grid {{ display: grid; grid-template-columns: 2.2fr 1fr; gap: 24px; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); padding: 12px 14px; margin-bottom: 12px; border-radius: 10px; }}
    ul {{ margin: 8px 0 0 18px; padding: 0; }}
    li {{ margin-bottom: 5px; line-height: 1.45; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .chip {{ border: 1px solid var(--line); border-radius: 999px; padding: 4px 10px; font-size: 12px; background: white; }}
  </style>
</head>
<body>
  <header>
    <h1>{_safe(contact_payload.get('name'))}</h1>
    <div class='subtle'>{_safe(contact_payload.get('email'))} | {_safe(contact_payload.get('phone'))} | {_safe(contact_payload.get('linkedin') or contact_payload.get('website'))}</div>
    <div class='lead'>{_safe(headline)}</div>
    <ul>{_bullet_list(summary_lines)}</ul>
  </header>
  <div class='grid'>
    <main>
      <h2>Experience</h2>
      {''.join(work_html) or '<p class="subtle">No work facts selected.</p>'}
      <h2>Projects</h2>
      {''.join(project_html) or '<p class="subtle">No project facts selected.</p>'}
      <h2>Targeting Notes</h2>
      <section class='card'>
        <p><strong>Target role:</strong> {_safe(evaluation.role)} at {_safe(evaluation.company)}</p>
        <p><strong>Archetype:</strong> {_safe(evaluation.archetype)}</p>
        <ul>{_bullet_list(custom_bullets)}</ul>
      </section>
    </main>
    <aside>
      <h2>Skills</h2>
      <section class='card'>
        <div class='chips'>{''.join(f"<span class='chip'>{_safe(name)}</span>" for name in skill_names if name)}</div>
      </section>
      <h2>Keywords</h2>
      <section class='card'>
        <ul>{_bullet_list(evaluation.keywords)}</ul>
      </section>
      <h2>Fit</h2>
      <section class='card'>
        <ul>{_bullet_list(evaluation.fit_reasons)}</ul>
      </section>
    </aside>
  </div>
</body>
</html>
"""


async def _render_pdf(html_path: Path, pdf_path: Path, paper_format: str) -> None:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(html_path.resolve().as_uri(), wait_until="load")
            await page.pdf(path=str(pdf_path), format=paper_format, print_background=True, margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
        finally:
            await browser.close()
    finally:
        await playwright.stop()


def _normalize_resume_renderer(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"chatgpt", "chatgpt_download"}:
        return _CHATGPT_DOWNLOAD_RENDERER
    if normalized == "latex_direct":
        return "latex_direct"
    if normalized == "latex":
        return "latex"
    if normalized in {"typst", ""}:
        return _DEFAULT_PDF_RENDERER
    if normalized in {"html", "html_to_pdf", "playwright"}:
        return _HTML_TO_PDF_RENDERER
    return _DEFAULT_PDF_RENDERER


def _template_bridge_details(ws: FileWorkspace) -> dict[str, Any]:
    details: dict[str, Any] = {
        'configured': True,
        'requested': False,
        'resume_renderer': _DEFAULT_PDF_RENDERER,
        'resume_template_path': None,
        'cover_letter_template_path': None,
        'missing_resume_template': False,
        'chatgpt_drafting': None,
    }
    config_path = ws.root / '.fmj' / 'config.toml'
    if not config_path.exists():
        return details
    config = AppConfig.load(ws.root)
    resume_renderer = _normalize_resume_renderer(config.personal.resume_renderer)
    resume_template = config.resume_template_path(ws.root)
    cover_template = config.cover_letter_template_path(ws.root)
    chatgpt_profile_dir = config.chatgpt_profile_dir(ws.root)
    chatgpt_downloads_dir = config.chatgpt_downloads_dir(ws.root)
    chatgpt_details = {
        'enabled': bool(config.chatgpt_drafting.enabled),
        'gpt_url': str(config.chatgpt_drafting.gpt_url or '').strip() or None,
        'completion_start_marker': config.chatgpt_drafting.completion_start_marker,
        'completion_end_marker': config.chatgpt_drafting.completion_end_marker,
        'profile_dir': chatgpt_profile_dir,
        'downloads_dir': chatgpt_downloads_dir,
        'browser_mode': config.chatgpt_drafting.browser_mode,
        'browser_cdp_url': config.chatgpt_drafting.browser_cdp_url,
        'launch_if_missing': bool(config.chatgpt_drafting.launch_if_missing),
        'use_temporary_chat': bool(config.chatgpt_drafting.use_temporary_chat),
        'timeout_seconds': int(config.chatgpt_drafting.timeout_seconds),
        'prompt_submit_delay_ms': int(config.chatgpt_drafting.prompt_submit_delay_ms),
        'download_timeout_seconds': int(config.chatgpt_drafting.download_timeout_seconds),
    }
    if resume_renderer == _CHATGPT_DOWNLOAD_RENDERER:
        details.update(
            {
                'requested': True,
                'configured': bool(
                    chatgpt_details['enabled']
                    and chatgpt_details['gpt_url']
                    and str(chatgpt_details['completion_start_marker']).strip()
                    and str(chatgpt_details['completion_end_marker']).strip()
                    and str(chatgpt_details['browser_cdp_url']).strip()
                ),
                'resume_renderer': _CHATGPT_DOWNLOAD_RENDERER,
                'chatgpt_drafting': chatgpt_details,
            }
        )
        return details
    if resume_renderer == 'latex_direct':
        details.update(
            {
                'requested': True,
                'configured': bool(
                    resume_template is not None
                    and cover_template is not None
                    and resume_template.exists()
                    and cover_template.exists()
                    and resume_template.suffix.casefold() == '.tex'
                    and cover_template.suffix.casefold() == '.tex'
                ),
                'resume_renderer': 'latex_direct',
                'resume_template_path': resume_template if resume_template is not None and resume_template.exists() else None,
                'cover_letter_template_path': cover_template if cover_template is not None and cover_template.exists() else None,
                'missing_resume_template': not bool(resume_template is not None and resume_template.exists()),
                'chatgpt_drafting': chatgpt_details,
            }
        )
        return details
    details.update(
        {
            'requested': resume_renderer == 'latex',
            'configured': resume_renderer in {_DEFAULT_PDF_RENDERER, _HTML_TO_PDF_RENDERER},
            'resume_renderer': resume_renderer,
            'resume_template_path': None,
            'cover_letter_template_path': cover_template if cover_template is not None and cover_template.exists() else None,
            'missing_resume_template': resume_renderer == 'latex' and resume_template is None,
            'chatgpt_drafting': chatgpt_details,
        }
    )
    if resume_renderer == 'latex' and resume_template is not None and not resume_template.exists():
        details['missing_resume_template'] = True
        details['configured'] = False
    elif resume_renderer == 'latex':
        details['resume_template_path'] = resume_template if resume_template is not None and resume_template.exists() else None
        details['configured'] = resume_template is not None and resume_template.exists()
    return details


def _build_pipeline_pdf_for_target(
    ws: FileWorkspace,
    job,
    evaluation,
    facts,
    profile,
    plan: ResumePlan,
    display_id: str,
    *,
    renderer: str,
    draft_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from findmyjob.core.enums import FactKind
    from findmyjob.core.types import ArtifactDraft, CoverLetterDraft, NormalizedJobPosting, ProfileFact, ResumeDraft
    from findmyjob.documents.pipeline import DocumentPipeline, DocumentTemplateConfig
    from findmyjob.sources.normalizer import normalize_text

    description_text = strip_html_tags(job.description or '')
    job_model = NormalizedJobPosting(
        company_name=evaluation.company or job.company,
        company_key=(evaluation.company or job.company).casefold().replace(' ', '-'),
        title=evaluation.role or job.title,
        source=job.source,
        source_kind=job.source_kind,
        source_job_id=job.source_job_id,
        posting_url=job.url,
        apply_url=job.apply_url,
        location_raw=job.location,
        description=description_text,
        normalized_description=normalize_text(description_text),
        discovered_at=datetime.now(timezone.utc),
        job_identity_key=f"{job.source}-{job.source_job_id}",
        duplicate_cluster_key=f"{job.source}-{job.source_job_id}",
    )

    profile_facts: list[ProfileFact] = []
    for fact in facts:
        profile_facts.append(
            ProfileFact(
                fact_id=fact.fact_id,
                kind=FactKind(fact.kind),
                payload=fact.payload,
                allowed_for_generation=fact.allowed_for_generation,
                disallowed=fact.disallowed,
                provenance=fact.provenance,
                confirmed=fact.confirmed,
            )
        )

    resume_draft = ResumeDraft(
        headline=_clean_generated_text(plan.headline or evaluation.resume_headline),
        summary_lines=_normalize_bullets(plan.summary_lines or evaluation.resume_summary_lines or []),
        selected_work_fact_ids=plan.selected_work_fact_ids or evaluation.selected_work_fact_ids,
        selected_project_fact_ids=plan.selected_project_fact_ids or evaluation.selected_project_fact_ids,
        selected_skill_fact_ids=plan.selected_skill_fact_ids or evaluation.selected_skill_fact_ids,
        custom_bullets=_normalize_bullets(plan.custom_bullets or evaluation.custom_bullets or []),
    )
    company_name = _display_company_name(evaluation.company or job.company)
    cover_paragraphs = _cover_letter_paragraphs(job, evaluation, plan.cover_letter_paragraphs or evaluation.cover_letter_paragraphs or [])
    draft_metadata = draft_metadata or {}
    cover_letter_draft = CoverLetterDraft(
        salutation=str(draft_metadata.get('salutation') or f'Dear {company_name} Hiring Team,'),
        paragraphs=cover_paragraphs,
        closing=str(draft_metadata.get('closing') or 'Sincerely,'),
        signature_name=_clean_generated_text(str(draft_metadata.get('signature_name') or profile.candidate.name)),
    )
    artifact_draft = ArtifactDraft(resume_draft=resume_draft, cover_letter_draft=cover_letter_draft)

    bridge = _template_bridge_details(ws)
    resume_template_path = bridge.get('resume_template_path')
    cover_letter_template_path = bridge.get('cover_letter_template_path')
    template_config = DocumentTemplateConfig(
        resume_renderer=renderer,
        resume_template_path=resume_template_path if renderer == 'latex' else None,
        cover_letter_template_path=cover_letter_template_path,
    )
    pipeline = DocumentPipeline(
        artifacts_dir=ws.output_dir,
        template_dir=ws.root / 'templates' / 'typst',
        template_config=template_config,
    )

    render_error = None
    warnings: list[str] = []
    try:
        artifacts = pipeline.build_application_artifacts(job_model, profile_facts, artifact_draft=artifact_draft)
        warnings = pipeline.validation_warnings(artifacts)
    except Exception as exc:
        render_error = str(exc)
        artifacts = []

    artifact_map = {artifact.path.name: artifact.path for artifact in artifacts}
    resume_pdf_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.resume.pdf')), None)
    cover_letter_pdf_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.cover_letter.pdf')), None)
    resume_text_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.resume.txt')), None)
    cover_letter_text_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.cover_letter.txt')), None)

    pdf_path = ws.resume_pdf_path_for(display_id, evaluation.company)
    if resume_pdf_path and resume_pdf_path.exists() and resume_pdf_path != pdf_path:
        import shutil

        shutil.copyfile(resume_pdf_path, pdf_path)
    elif resume_pdf_path and resume_pdf_path.exists():
        pdf_path = resume_pdf_path

    cover_letter_path = ws.output_dir / f"cover-letter-{display_id}-{evaluation.company.casefold().replace(' ', '-')}.pdf"
    if cover_letter_pdf_path and cover_letter_pdf_path.exists() and cover_letter_pdf_path != cover_letter_path:
        import shutil

        shutil.copyfile(cover_letter_pdf_path, cover_letter_path)
    elif cover_letter_pdf_path and cover_letter_pdf_path.exists():
        cover_letter_path = cover_letter_pdf_path

    return {
        'success': pdf_path.exists() and render_error is None,
        'renderer': renderer,
        'template_bridge_used': renderer == 'latex' and bridge.get('configured', False) and resume_template_path is not None,
        'resume_template_path': resume_template_path if renderer == 'latex' else None,
        'cover_letter_template_path': cover_letter_template_path,
        'pdf_path': pdf_path if pdf_path.exists() else None,
        'cover_letter_path': cover_letter_path if cover_letter_path.exists() else None,
        'resume_text_path': resume_text_path if resume_text_path is not None and resume_text_path.exists() else None,
        'cover_letter_text_path': cover_letter_text_path if cover_letter_text_path is not None and cover_letter_text_path.exists() else None,
        'render_error': render_error,
        'warnings': warnings,
        'artifacts': {name: str(path) for name, path in artifact_map.items()},
    }

def build_pdf_for_target(workspace: Path | FileWorkspace, target: str) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    application = ws.find_application(target)
    job_id = application.job_id if application is not None else target
    job = ws.load_job(job_id)
    if job is None:
        raise ValueError(f"Unknown PDF target: {target}")
    evaluation = ws.load_evaluation(job.job_id)
    if evaluation is None:
        raise ValueError(f"No evaluation found for job: {job.job_id}")

    facts = ws.load_facts()
    profile = ws.load_profile()
    display_id = application.id if application is not None else ws.next_application_id()

    bridge = _template_bridge_details(ws)
    active_renderer = str(bridge.get('resume_renderer') or _DEFAULT_PDF_RENDERER)
    draft_metadata: dict[str, Any] = {}
    plan = ResumePlan()
    if bridge.get('requested') and not bridge.get('configured'):
        missing_resume_template = bool(bridge.get('missing_resume_template'))
        renderer_label = active_renderer if active_renderer in {'latex_direct', _CHATGPT_DOWNLOAD_RENDERER} else 'latex'
        render_error = (
            f'{renderer_label} resume rendering is configured in .fmj/config.toml, but the local resume template is missing.'
            if missing_resume_template
            else (
                'chatgpt_download resume rendering is configured in .fmj/config.toml, but the ChatGPT drafting configuration is incomplete.'
                if renderer_label == _CHATGPT_DOWNLOAD_RENDERER
                else f'{renderer_label} resume rendering is configured in .fmj/config.toml, but the template bridge is incomplete.'
            )
        )
        if application is not None:
            updated = application.model_copy(update={'pdf': False, 'status': application.status})
            ws.upsert_application(updated)
        ws.update_inbox_state(job.job_id, 'evaluated')
        return {
            'success': False,
            'job_id': job.job_id,
            'application_id': display_id,
            'renderer': renderer_label,
            'template_bridge_used': False,
            'resume_template_path': None,
            'cover_letter_template_path': ws.relative_path(bridge['cover_letter_template_path']) if bridge.get('cover_letter_template_path') else None,
            'html_path': None,
            'pdf_path': None,
            'cover_letter_path': None,
            'resume_text_path': None,
            'cover_letter_text_path': None,
            'warnings': [],
            'render_error': render_error,
            'draft': draft_metadata,
        }
    if active_renderer == _CHATGPT_DOWNLOAD_RENDERER:
        service = ChatGPTDraftingService(ws)
        result = service.draft(
            job=job,
            evaluation=evaluation,
            application_id=display_id,
            on_date=application.date if application is not None else None,
        )
        render_error = str(result.get('render_error') or '').strip() or None
        pdf_ok = bool(result.get('pdf_path')) and render_error is None
        if application is not None:
            updated = application.model_copy(update={'pdf': pdf_ok, 'status': 'PDF Ready' if pdf_ok else application.status, 'notes': render_error})
            ws.upsert_application(updated)
        ws.update_inbox_state(job.job_id, 'pdf_ready' if pdf_ok else 'evaluated')
        return result
    if active_renderer == 'latex_direct':
        from findmyjob.documents.pipeline import DocumentPipeline, DocumentTemplateConfig
        from findmyjob.filefirst.advanced_models import load_model_router

        router = load_model_router(ws)
        if router is None:
            render_error = 'No advanced model router is configured for latex_direct rendering.'
            if application is not None:
                updated = application.model_copy(update={'pdf': False, 'status': application.status})
                ws.upsert_application(updated)
            ws.update_inbox_state(job.job_id, 'evaluated')
            return {
                'success': False,
                'job_id': job.job_id,
                'application_id': display_id,
                'renderer': 'latex_direct',
                'template_bridge_used': False,
                'resume_template_path': ws.relative_path(bridge['resume_template_path']) if bridge.get('resume_template_path') else None,
                'cover_letter_template_path': ws.relative_path(bridge['cover_letter_template_path']) if bridge.get('cover_letter_template_path') else None,
                'html_path': None,
                'pdf_path': None,
                'cover_letter_path': None,
                'resume_text_path': None,
                'cover_letter_text_path': None,
                'warnings': [],
                'render_error': render_error,
                'draft': draft_metadata,
            }

        from datetime import datetime, timezone

        from findmyjob.core.enums import FactKind
        from findmyjob.core.types import NormalizedJobPosting, ProfileFact
        from findmyjob.sources.normalizer import normalize_text

        description_text = strip_html_tags(job.description or '')
        job_model = NormalizedJobPosting(
            company_name=evaluation.company or job.company,
            company_key=(evaluation.company or job.company).casefold().replace(' ', '-'),
            title=evaluation.role or job.title,
            source=job.source,
            source_kind=job.source_kind,
            source_job_id=job.source_job_id,
            posting_url=job.url,
            apply_url=job.apply_url,
            location_raw=job.location,
            description=description_text,
            normalized_description=normalize_text(description_text),
            discovered_at=datetime.now(timezone.utc),
            job_identity_key=f"{job.source}-{job.source_job_id}",
            duplicate_cluster_key=f"{job.source}-{job.source_job_id}",
        )
        profile_facts = [
            ProfileFact(
                fact_id=fact.fact_id,
                kind=FactKind(fact.kind),
                payload=fact.payload,
                allowed_for_generation=fact.allowed_for_generation,
                disallowed=fact.disallowed,
                provenance=fact.provenance,
                confirmed=fact.confirmed,
            )
            for fact in facts
        ]
        template_config = DocumentTemplateConfig(
            resume_renderer='latex_direct',
            resume_template_path=bridge.get('resume_template_path'),
            cover_letter_template_path=bridge.get('cover_letter_template_path'),
        )
        pipeline = DocumentPipeline(
            artifacts_dir=ws.output_dir,
            template_dir=ws.root / 'templates' / 'typst',
            template_config=template_config,
        )

        render_error = None
        warnings: list[str] = []
        try:
            from functools import partial

            artifacts = run_async(
                partial(
                    pipeline.build_application_artifacts_latex_direct,
                    job_model,
                    profile_facts,
                    router,
                    resume_template_path=template_config.resume_template_path,
                    cover_letter_template_path=template_config.cover_letter_template_path,
                )
            )
            warnings = pipeline.validation_warnings(artifacts)
        except Exception as exc:
            render_error = str(exc)
            artifacts = []

        artifact_map = {artifact.path.name: artifact.path for artifact in artifacts}
        resume_pdf_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.resume.pdf')), None)
        cover_letter_pdf_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.cover_letter.pdf')), None)
        resume_text_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.resume.txt')), None)
        cover_letter_text_path = next((artifact.path for artifact in artifacts if artifact.path.name.endswith('.cover_letter.txt')), None)

        pdf_path = ws.resume_pdf_path_for(display_id, evaluation.company)
        if resume_pdf_path is not None and resume_pdf_path.exists() and resume_pdf_path != pdf_path:
            import shutil

            shutil.copyfile(resume_pdf_path, pdf_path)
        elif resume_pdf_path is not None and resume_pdf_path.exists():
            pdf_path = resume_pdf_path

        cover_letter_path = ws.output_dir / f"cover-letter-{display_id}-{evaluation.company.casefold().replace(' ', '-')}.pdf"
        if cover_letter_pdf_path is not None and cover_letter_pdf_path.exists() and cover_letter_pdf_path != cover_letter_path:
            import shutil

            shutil.copyfile(cover_letter_pdf_path, cover_letter_path)
        elif cover_letter_pdf_path is not None and cover_letter_pdf_path.exists():
            cover_letter_path = cover_letter_pdf_path

        if draft_metadata.get('writer_profile'):
            warnings.append(f"writer_profile={draft_metadata['writer_profile']}")
        if draft_metadata.get('validation_profile'):
            warnings.append(f"validation_profile={draft_metadata['validation_profile']}")

        pdf_ok = pdf_path.exists() and render_error is None
        if application is not None:
            updated = application.model_copy(update={'pdf': pdf_ok, 'status': 'PDF Ready' if pdf_ok else application.status})
            ws.upsert_application(updated)
        ws.update_inbox_state(job.job_id, 'pdf_ready' if pdf_ok else 'evaluated')
        return {
            'success': pdf_ok,
            'job_id': job.job_id,
            'application_id': display_id,
            'renderer': 'latex_direct',
            'template_bridge_used': bool(bridge.get('configured')),
            'resume_template_path': ws.relative_path(template_config.resume_template_path) if template_config.resume_template_path else None,
            'cover_letter_template_path': ws.relative_path(template_config.cover_letter_template_path) if template_config.cover_letter_template_path else None,
            'html_path': None,
            'pdf_path': ws.relative_path(Path(pdf_path)) if pdf_ok else None,
            'cover_letter_path': ws.relative_path(Path(cover_letter_path)) if cover_letter_pdf_path is not None and cover_letter_path.exists() else None,
            'resume_text_path': ws.relative_path(resume_text_path) if resume_text_path is not None and resume_text_path.exists() else None,
            'cover_letter_text_path': ws.relative_path(cover_letter_text_path) if cover_letter_text_path is not None and cover_letter_text_path.exists() else None,
            'warnings': warnings,
            'render_error': render_error,
            'draft': draft_metadata,
            'artifacts': {name: str(path) for name, path in artifact_map.items()},
        }
    from findmyjob.filefirst.drafting import build_resume_plan_with_router

    plan, draft_metadata = build_resume_plan_with_router(ws, job, evaluation)
    use_pipeline_renderer = active_renderer in {'latex', 'typst'}

    if use_pipeline_renderer:
        pipeline_result = _build_pipeline_pdf_for_target(
            ws,
            job,
            evaluation,
            facts,
            profile,
            plan,
            display_id,
            renderer=active_renderer,
            draft_metadata=draft_metadata,
        )
        pdf_path = pipeline_result.get('pdf_path')
        cover_letter_path = pipeline_result.get('cover_letter_path')
        render_error = pipeline_result.get('render_error')
        warnings = list(pipeline_result.get('warnings') or [])
        writer_profile = draft_metadata.get('writer_profile')
        validation_profile = draft_metadata.get('validation_profile')
        if writer_profile:
            warnings.append(f'writer_profile={writer_profile}')
        if validation_profile:
            warnings.append(f'validation_profile={validation_profile}')

        pdf_ok = pdf_path is not None and Path(pdf_path).exists()
        if application is not None:
            updated = application.model_copy(update={'pdf': pdf_ok, 'status': 'PDF Ready' if pdf_ok else application.status})
            ws.upsert_application(updated)
        ws.update_inbox_state(job.job_id, 'pdf_ready' if pdf_ok else 'evaluated')
        return {
            'success': pdf_ok and render_error is None,
            'job_id': job.job_id,
            'application_id': display_id,
            'renderer': pipeline_result.get('renderer'),
            'template_bridge_used': bool(pipeline_result.get('template_bridge_used')),
            'resume_template_path': ws.relative_path(pipeline_result['resume_template_path']) if pipeline_result.get('resume_template_path') else None,
            'cover_letter_template_path': ws.relative_path(pipeline_result['cover_letter_template_path']) if pipeline_result.get('cover_letter_template_path') else None,
            'html_path': None,
            'pdf_path': ws.relative_path(Path(pdf_path)) if pdf_ok else None,
            'cover_letter_path': ws.relative_path(Path(cover_letter_path)) if cover_letter_path is not None and Path(cover_letter_path).exists() else None,
            'resume_text_path': ws.relative_path(pipeline_result['resume_text_path']) if pipeline_result.get('resume_text_path') else None,
            'cover_letter_text_path': ws.relative_path(pipeline_result['cover_letter_text_path']) if pipeline_result.get('cover_letter_text_path') else None,
            'warnings': warnings,
            'render_error': render_error,
            'draft': draft_metadata,
        }

    terms = _terms(job, evaluation)
    contact = _contact_fact(facts)
    work = _select_facts(facts, 'work', plan.selected_work_fact_ids or evaluation.selected_work_fact_ids, terms, 4)
    projects = _select_facts(facts, 'project', plan.selected_project_fact_ids or evaluation.selected_project_fact_ids, terms, 3)
    skills = _select_facts(facts, 'skill', plan.selected_skill_fact_ids or evaluation.selected_skill_fact_ids, terms, 12)
    html_body = _render_resume_html(profile, contact, work, projects, skills, evaluation, plan)

    html_path = ws.resume_html_path_for(display_id, evaluation.company)
    pdf_path = ws.resume_pdf_path_for(display_id, evaluation.company)
    html_path.write_text(html_body, encoding='utf-8')

    cover_letter_path = ws.output_dir / f"cover-letter-{display_id}-{evaluation.company.casefold().replace(' ', '-')}.md"
    cover_lines = [
        f"# Cover Letter - {evaluation.company}",
        '',
        draft_metadata.get('salutation') or f"Dear {evaluation.company} Hiring Team,",
        '',
        *[paragraph + '\n' for paragraph in _cover_letter_paragraphs(job, evaluation, plan.cover_letter_paragraphs or evaluation.cover_letter_paragraphs)],
        draft_metadata.get('closing') or 'Sincerely,',
        draft_metadata.get('signature_name') or profile.candidate.name,
    ]
    cover_letter_path.write_text('\n'.join(str(line).rstrip() for line in cover_lines if str(line).strip() or line == '').rstrip() + '\n', encoding='utf-8')

    render_error = None
    paper_format = 'Letter' if any(code == 'US' for code in profile.targets.countries) else 'A4'
    try:
        run_async(_render_pdf, html_path, pdf_path, paper_format)
    except Exception as exc:
        render_error = str(exc)

    pdf_ok = pdf_path.exists()
    if application is not None:
        updated = application.model_copy(update={'pdf': pdf_ok, 'status': 'PDF Ready' if pdf_ok else application.status})
        ws.upsert_application(updated)
    ws.update_inbox_state(job.job_id, 'pdf_ready' if pdf_ok else 'evaluated')
    warnings = []
    if draft_metadata.get('writer_profile'):
        warnings.append(f"writer_profile={draft_metadata['writer_profile']}")
    if draft_metadata.get('validation_profile'):
        warnings.append(f"validation_profile={draft_metadata['validation_profile']}")
    return {
        'success': pdf_ok and render_error is None,
        'job_id': job.job_id,
        'application_id': display_id,
        'renderer': _HTML_TO_PDF_RENDERER,
        'template_bridge_used': False,
        'resume_template_path': None,
        'cover_letter_template_path': ws.relative_path(bridge['cover_letter_template_path']) if bridge.get('cover_letter_template_path') else None,
        'html_path': ws.relative_path(html_path),
        'pdf_path': ws.relative_path(pdf_path) if pdf_ok else None,
        'cover_letter_path': ws.relative_path(cover_letter_path),
        'resume_text_path': ws.relative_path(html_path),
        'cover_letter_text_path': ws.relative_path(cover_letter_path),
        'warnings': warnings,
        'render_error': render_error,
        'draft': draft_metadata,
    }
