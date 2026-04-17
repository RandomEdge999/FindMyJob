from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from findmyjob.core.tooling import find_latex_engine, find_typst_executable

from findmyjob.core.types import ArtifactDraft, NormalizedJobPosting, ProfileFact
from findmyjob.db.repositories import hash_content
from findmyjob.filefirst.text_utils import drop_trailing_single_character_lines, strip_html_tags
from findmyjob.sources.normalizer import normalize_text, slugify

_RENDER_TEXT_SANITIZER = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "- ",
        "\u2026": "...",
        "\u00a0": " ",
        "\ufeff": "",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\u2060": "",
    }
)

_LATEX_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _normalize_render_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_RENDER_TEXT_SANITIZER)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if char in {"\n", "\t"} or ord(char) >= 32)
    return text.strip()


def _normalize_render_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {key: _normalize_render_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_normalize_render_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return [_normalize_render_payload(item) for item in payload]
    if isinstance(payload, str):
        return _normalize_render_text(payload)
    return payload


def _latex_escape(value: Any, *, preserve_newlines: bool = False) -> str:
    text = _normalize_render_text(value)
    if not preserve_newlines:
        text = text.replace("\n", " ")
    return "".join(_LATEX_ESCAPE_MAP.get(char, char) for char in text)


def _latex_href_target(value: Any) -> str:
    text = _normalize_render_text(value).replace("\n", "")
    return text.replace("{", "").replace("}", "")


@dataclass(slots=True)
class DocumentTemplateConfig:
    resume_renderer: str = 'typst'
    resume_template_path: Path | None = None
    cover_letter_template_path: Path | None = None
    cover_letter_reference_path: Path | None = None


@dataclass(slots=True)
class RenderedArtifact:
    kind: str
    path: Path
    content_hash: str
    validation_results: dict[str, Any]


class DocumentPipeline:
    def __init__(self, artifacts_dir: Path, template_dir: Path, template_config: DocumentTemplateConfig | None = None) -> None:
        self.artifacts_dir = artifacts_dir
        self.template_dir = template_dir
        self.template_config = template_config or DocumentTemplateConfig()
        self._raw_latex_validation_context: dict[str, Any] | None = None
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    @property
    def resume_renderer(self) -> str:
        return self.template_config.resume_renderer or 'typst'

    def inspect_template_state(self) -> dict[str, Any]:
        return {
            'resume_renderer': self.resume_renderer,
            'resume_template_path': str(self.template_config.resume_template_path) if self.template_config.resume_template_path else None,
            'cover_letter_template_path': str(self.template_config.cover_letter_template_path) if self.template_config.cover_letter_template_path else None,
            'cover_letter_reference_path': str(self.template_config.cover_letter_reference_path) if self.template_config.cover_letter_reference_path else None,
            'typst_template_dir': str(self.template_dir),
        }

    def _latex_subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        miktex_root = self.artifacts_dir / '.miktex'
        config_dir = miktex_root / 'config'
        data_dir = miktex_root / 'data'
        install_dir = miktex_root / 'install'
        for path in (config_dir, data_dir, install_dir):
            path.mkdir(parents=True, exist_ok=True)
        env.setdefault('MIKTEX_USERCONFIG', str(config_dir.resolve()))
        env.setdefault('MIKTEX_USERDATA', str(data_dir.resolve()))
        env.setdefault('MIKTEX_USERINSTALL', str(install_dir.resolve()))
        env.setdefault('TEXMFCONFIG', str(config_dir.resolve()))
        env.setdefault('TEXMFVAR', str(data_dir.resolve()))
        return env

    def build_resume_context(self, job: NormalizedJobPosting, facts: list[ProfileFact], artifact_draft: ArtifactDraft | None = None) -> dict[str, Any]:
        sections: dict[str, list[dict[str, Any]]] = {
            'contact': [],
            'education': [],
            'work': [],
            'projects': [],
            'skills': [],
            'preferences': [],
        }
        facts_by_id = {fact.fact_id: fact for fact in facts}
        job_terms = self._job_terms(job)
        scored_work: list[tuple[int, dict[str, Any]]] = []
        scored_projects: list[tuple[int, dict[str, Any]]] = []
        scored_skills: list[tuple[int, dict[str, Any]]] = []

        for fact in facts:
            if fact.disallowed:
                continue
            payload = {**fact.payload, 'fact_id': fact.fact_id}
            if fact.kind.value == 'contact':
                sections['contact'].append(payload)
            elif fact.kind.value == 'education':
                sections['education'].append(payload)
            elif fact.kind.value == 'work':
                scored_work.append((self._score_payload(payload, job_terms), payload))
            elif fact.kind.value == 'project':
                scored_projects.append((self._score_payload(payload, job_terms), payload))
            elif fact.kind.value == 'skill':
                scored_skills.append((self._score_payload(payload, job_terms), payload))
            else:
                sections['preferences'].append(payload)

        selected_work_ids = list(artifact_draft.resume_draft.selected_work_fact_ids) if artifact_draft is not None else []
        selected_project_ids = list(artifact_draft.resume_draft.selected_project_fact_ids) if artifact_draft is not None else []
        selected_skill_ids = list(artifact_draft.resume_draft.selected_skill_fact_ids) if artifact_draft is not None else []

        def _selected_payloads(selected_ids: list[str], kind: str) -> list[dict[str, Any]]:
            selected: list[dict[str, Any]] = []
            for fact_id in selected_ids:
                fact = facts_by_id.get(fact_id)
                if fact is None or fact.disallowed or fact.kind.value != kind:
                    continue
                selected.append({**fact.payload, 'fact_id': fact.fact_id})
            return selected

        sections['work'] = _selected_payloads(selected_work_ids, 'work') or [payload for _score, payload in sorted(scored_work, key=lambda item: item[0], reverse=True)[:3]]
        sections['projects'] = _selected_payloads(selected_project_ids, 'project') or [payload for _score, payload in sorted(scored_projects, key=lambda item: item[0], reverse=True)[:2]]
        sections['skills'] = _selected_payloads(selected_skill_ids, 'skill') or [payload for _score, payload in sorted(scored_skills, key=lambda item: item[0], reverse=True)[:8]]

        self._validate_required_contact(sections['contact'])
        selected_fact_ids = [item['fact_id'] for section in ('work', 'projects', 'skills') for item in sections[section] if item.get('fact_id')]
        context = {
            'job': job.model_dump(mode='json'),
            'profile': sections,
            'selected_fact_ids': selected_fact_ids,
            'template_state': self.inspect_template_state(),
        }
        if artifact_draft is not None:
            context['resume_draft'] = artifact_draft.resume_draft.model_dump(mode='json')
            context['artifact_draft'] = artifact_draft.model_dump(mode='json')
        return context

    def deterministic_base_name(self, job: NormalizedJobPosting) -> str:
        return f"{slugify(job.company_name)}-{slugify(job.title)}-{job.source}-{job.source_job_id}"

    def write_context(self, base_name: str, context: dict[str, Any]) -> RenderedArtifact:
        path = self.artifacts_dir / f'{base_name}.context.json'
        body = json.dumps(context, indent=2, sort_keys=True)
        path.write_text(body, encoding='utf-8')
        return RenderedArtifact(kind='context', path=path, content_hash=hash_content(context), validation_results={'written': True, 'selected_fact_ids': context.get('selected_fact_ids', []), 'valid': True})

    def write_resume_text(self, base_name: str, context: dict[str, Any]) -> RenderedArtifact:
        path = self.artifacts_dir / f'{base_name}.resume.txt'
        contact = self._required_contact(context)
        work = context['profile'].get('work', [])
        projects = context['profile'].get('projects', [])
        skills = context['profile'].get('skills', [])
        draft = context.get('resume_draft') or {}
        lines = [str(contact['name']), str(contact['email'])]
        for key in ('phone', 'linkedin', 'portfolio', 'website'):
            if contact.get(key):
                lines.append(str(contact[key]))
        if draft.get('headline'):
            lines.append(str(draft['headline']))
        lines.append(context['job']['title'])
        for line in draft.get('summary_lines', [])[:4]:
            if str(line).strip():
                lines.append(normalize_text(str(line)))
        for entry in work:
            summary = normalize_text(str(entry.get('summary') or entry.get('description') or entry.get('achievement') or ''))
            if summary:
                lines.append(summary)
        for entry in projects:
            summary = normalize_text(str(entry.get('summary') or entry.get('description') or entry.get('achievement') or ''))
            if summary:
                lines.append(summary)
        for bullet in draft.get('custom_bullets', [])[:4]:
            bullet_text = normalize_text(str(bullet or ''))
            if bullet_text:
                lines.append(bullet_text)
        if skills:
            skill_names = [str(entry.get('name', '')).strip() for entry in skills if entry.get('name')]
            if skill_names:
                lines.append('Skills: ' + ', '.join(skill_names))
        filtered = [line for line in lines if line]
        cleaned_text = drop_trailing_single_character_lines('\n'.join(filtered))
        cleaned_lines = [line for line in cleaned_text.splitlines() if line]
        path.write_text(cleaned_text + ('\n' if cleaned_text else ''), encoding='utf-8')
        validation = self.validate_plain_text(cleaned_lines, context)
        return RenderedArtifact(kind='resume', path=path, content_hash=hash_content(cleaned_lines), validation_results=validation)

    def write_cover_letter_text(self, base_name: str, context: dict[str, Any]) -> RenderedArtifact:
        path = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        cover_letter = context.get('cover_letter') or self.build_cover_letter_payload(context)
        context['cover_letter'] = cover_letter
        contact = self._required_contact(context)
        education = list((context.get('profile') or {}).get('education') or [])
        first_education = education[0] if education else {}
        lines: list[str] = []
        lines.append(str(cover_letter['salutation']))
        lines.append('')
        for paragraph in cover_letter.get('paragraphs', []):
            lines.append(str(paragraph))
            lines.append('')
        lines.append(str(cover_letter.get('closing') or 'Sincerely,'))
        lines.append('')
        lines.append(str(cover_letter.get('signature_name') or contact['name']))
        education_line = ', '.join(
            str(part).strip()
            for part in (
                first_education.get('school'),
                first_education.get('degree'),
                first_education.get('date_label') or first_education.get('dates'),
            )
            if str(part or '').strip()
        )
        if education_line:
            lines.append(education_line)
        contact_line = ' | '.join(
            str(part).strip()
            for part in (contact.get('phone'), contact.get('email'))
            if str(part or '').strip()
        )
        if contact_line:
            lines.append(contact_line)
        links_line = ' | '.join(
            str(part).strip()
            for part in (
                contact.get('linkedin'),
                contact.get('github'),
            )
            if str(part or '').strip()
        )
        if links_line:
            lines.append(links_line)
        website = contact.get('portfolio') or contact.get('website')
        if str(website or '').strip():
            lines.append(str(website).strip())
        path.write_text('\n'.join(lines).strip() + '\n', encoding='utf-8')
        validation = self.validate_cover_letter_text(lines, context)
        return RenderedArtifact(kind='cover_letter', path=path, content_hash=hash_content(lines), validation_results=validation)

    def render_typst(self, template_name: str, base_name: str, context: dict[str, Any]) -> RenderedArtifact:
        template_path = self.template_dir / template_name
        output_path = self.artifacts_dir / f"{base_name}.{template_name.replace('.typ', '')}.pdf"
        typst = find_typst_executable()
        if not typst:
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results={'valid': False, 'failure_reason': 'typst_not_installed', 'selected_fact_ids': context.get('selected_fact_ids', [])})
        if not template_path.exists():
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results={'valid': False, 'failure_reason': f'missing_template:{template_name}', 'selected_fact_ids': context.get('selected_fact_ids', [])})
        context_path = self.artifacts_dir / f'{base_name}.render.json'
        normalized_context = _normalize_render_payload(context)
        context_path.write_text(json.dumps(normalized_context, indent=2, sort_keys=True, ensure_ascii=False), encoding='utf-8')
        command = [
            typst,
            'compile',
            '--root',
            str(self._typst_project_root(template_path, context_path, output_path)),
            str(template_path),
            str(output_path),
            f'--input=context={self._typst_context_argument(template_path, context_path)}',
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        validation = {'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr, 'selected_fact_ids': context.get('selected_fact_ids', [])}
        if completed.returncode != 0 or not output_path.exists():
            validation['valid'] = False
            validation['failure_reason'] = 'typst_compile_failed'
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results=validation)
        if template_name == 'resume.typ':
            fitted_context = self._shrink_context_until_one_page(output_path, context, base_name, template_name, typst)
            if fitted_context is not context:
                validation['shrink_applied'] = True
                context = fitted_context
        validation.update(self.validate_pdf(output_path, expect_one_page=template_name == 'resume.typ', context=context))
        return RenderedArtifact(kind='pdf', path=output_path, content_hash=hash_content(output_path.read_bytes().hex()), validation_results=validation)

    def render_latex_resume(self, base_name: str, context: dict[str, Any]) -> RenderedArtifact:
        output_path = self.artifacts_dir / f'{base_name}.resume.pdf'
        source_path = self.template_config.resume_template_path
        engine = find_latex_engine()
        if source_path is None and not context.get('resume_draft'):
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results={'valid': False, 'failure_reason': 'missing_resume_template', 'selected_fact_ids': context.get('selected_fact_ids', [])})
        if source_path is not None and not source_path.exists() and not context.get('resume_draft'):
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results={'valid': False, 'failure_reason': 'missing_resume_template_file', 'selected_fact_ids': context.get('selected_fact_ids', [])})
        if not engine:
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results={'valid': False, 'failure_reason': 'latex_not_installed', 'selected_fact_ids': context.get('selected_fact_ids', [])})
        build_dir = self.artifacts_dir / f'{base_name}.latex'
        shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True, exist_ok=True)
        if context.get('resume_draft'):
            active_source = build_dir / 'autonomous_resume.tex'
            active_source.write_text(self._build_latex_resume_source(context), encoding='utf-8')
        else:
            active_source = source_path
        command = [engine, '-interaction=nonstopmode', '-halt-on-error', '-file-line-error', '-output-directory', str(build_dir), str(active_source)]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=self._latex_subprocess_env(),
        )
        candidate_pdf = build_dir / f'{active_source.stem}.pdf'
        validation = {'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr, 'selected_fact_ids': context.get('selected_fact_ids', [])}
        if completed.returncode != 0 or not candidate_pdf.exists():
            validation['valid'] = False
            validation['failure_reason'] = 'latex_compile_failed'
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results=validation)
        shutil.copyfile(candidate_pdf, output_path)
        validation.update(self.validate_pdf(output_path, expect_one_page=True, context=context))
        return RenderedArtifact(kind='pdf', path=output_path, content_hash=hash_content(output_path.read_bytes().hex()), validation_results=validation)

    def write_resume_text_from_pdf(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict[str, Any]) -> RenderedArtifact:
        path = self.artifacts_dir / f'{base_name}.resume.txt'
        validation = {'selected_fact_ids': context.get('selected_fact_ids', [])}
        if not pdf_artifact.validation_results.get('valid') or not pdf_artifact.path.exists():
            path.write_text('', encoding='utf-8')
            validation['valid'] = False
            validation['failure_reason'] = pdf_artifact.validation_results.get('failure_reason') or 'resume_pdf_unavailable'
            validation['text_length'] = 0
            return RenderedArtifact(kind='resume', path=path, content_hash=hash_content(''), validation_results=validation)
        text = self.extract_pdf_text(pdf_artifact.path)
        path.write_text(text, encoding='utf-8')
        validation.update(self.validate_resume_text_extraction(text, context))
        return RenderedArtifact(kind='resume', path=path, content_hash=hash_content(text), validation_results=validation)

    def write_cover_letter_text_from_pdf(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict[str, Any]) -> RenderedArtifact:
        path = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        validation = {'selected_fact_ids': context.get('selected_fact_ids', [])}
        if not pdf_artifact.validation_results.get('valid') or not pdf_artifact.path.exists():
            path.write_text('', encoding='utf-8')
            validation['valid'] = False
            validation['failure_reason'] = pdf_artifact.validation_results.get('failure_reason') or 'cover_letter_pdf_unavailable'
            validation['text_length'] = 0
            return RenderedArtifact(kind='cover_letter', path=path, content_hash=hash_content(''), validation_results=validation)
        text = self.extract_pdf_text(pdf_artifact.path)
        path.write_text(text, encoding='utf-8')
        validation.update(self.validate_cover_letter_text(text.splitlines(), context))
        return RenderedArtifact(kind='cover_letter', path=path, content_hash=hash_content(text), validation_results=validation)

    def extract_pdf_text(self, pdf_path: Path) -> str:
        reader = PdfReader(str(pdf_path))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        return drop_trailing_single_character_lines(text)

    def validate_pdf(self, pdf_path: Path, expect_one_page: bool, context: dict[str, Any]) -> dict[str, Any]:
        reader = PdfReader(str(pdf_path))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        contact = self._required_contact(context)
        missing_contact = [key for key in ('name', 'email') if str(contact[key]) not in text]
        one_page_ok = (len(reader.pages) == 1) if expect_one_page else True
        contains_placeholder = '{{' in text or 'TODO' in text or '{job_title}' in text or '{company_name}' in text
        text_length = len(text)
        valid = one_page_ok and not contains_placeholder and not missing_contact and text_length > 0
        failure_reason = None
        if not one_page_ok:
            failure_reason = 'resume_exceeds_one_page'
        elif contains_placeholder:
            failure_reason = 'pdf_contains_placeholder'
        elif missing_contact:
            failure_reason = 'pdf_missing_contact_fields'
        elif text_length <= 0:
            failure_reason = 'pdf_text_extraction_failed'
        overflow_line_count = self._pdf_overflow_line_count(pdf_path) if not one_page_ok else 0
        return {'page_count': len(reader.pages), 'one_page_ok': one_page_ok, 'overflow_line_count': overflow_line_count, 'contains_placeholder': contains_placeholder, 'missing_contact_fields': missing_contact, 'text_length': text_length, 'valid': valid, 'failure_reason': failure_reason}

    def validate_plain_text(self, lines: list[str], context: dict[str, Any]) -> dict[str, Any]:
        joined = '\n'.join(lines)
        supported_snippets = []
        for section in ('work', 'projects'):
            for item in context['profile'].get(section, []):
                summary = normalize_text(str(item.get('summary') or item.get('description') or item.get('achievement') or ''))
                if summary:
                    supported_snippets.append(summary)
        draft = context.get('resume_draft') or {}
        headline = normalize_text(str(draft.get('headline') or ''))
        if headline:
            supported_snippets.append(headline)
        for line in draft.get('summary_lines', []) or []:
            summary_line = normalize_text(str(line or ''))
            if summary_line:
                supported_snippets.append(summary_line)
        for line in draft.get('custom_bullets', []) or []:
            custom_line = normalize_text(str(line or ''))
            if custom_line:
                supported_snippets.append(custom_line)
        contact = self._required_contact(context)
        allowed_boilerplate = {
            f"Dear Hiring Team at {context['job']['company_name']},",
            f"I am applying for the {context['job']['title']} role.",
            'Thank you for your consideration.',
            context['job']['title'],
            contact['name'],
            contact['email'],
            str(contact.get('phone') or ''),
            str(contact.get('linkedin') or ''),
            str(contact.get('portfolio') or ''),
            str(contact.get('website') or ''),
        }
        unsupported = [line for line in lines if line and line not in supported_snippets and not line.startswith('Skills:') and line not in allowed_boilerplate and not line.startswith('http') and not line.startswith('+') and '@' not in line]
        contains_placeholder = '{{' in joined or 'TODO' in joined
        valid = not contains_placeholder and not unsupported and len(joined.strip()) > 0
        failure_reason = None
        if contains_placeholder:
            failure_reason = 'plain_text_contains_placeholder'
        elif unsupported:
            failure_reason = 'plain_text_contains_unsupported_lines'
        elif not joined.strip():
            failure_reason = 'plain_text_empty'
        return {'contains_placeholder': contains_placeholder, 'unsupported_lines': unsupported, 'selected_fact_ids': context.get('selected_fact_ids', []), 'text_length': len(joined), 'valid': valid, 'failure_reason': failure_reason}

    def validate_resume_text_extraction(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        contact = self._required_contact(context)
        missing_contact = [field for field in ('name', 'email') if str(contact[field]) not in text]
        contains_placeholder = '{{' in text or 'TODO' in text or '{job_title}' in text or '{company_name}' in text
        valid = not contains_placeholder and not missing_contact and len(text.strip()) > 0
        failure_reason = None
        if contains_placeholder:
            failure_reason = 'resume_text_contains_placeholder'
        elif missing_contact:
            failure_reason = 'resume_text_missing_contact_fields'
        elif not text.strip():
            failure_reason = 'resume_text_empty'
        return {'contains_placeholder': contains_placeholder, 'missing_contact_fields': missing_contact, 'text_length': len(text), 'valid': valid, 'failure_reason': failure_reason}

    def validate_cover_letter_text(self, lines: list[str], context: dict[str, Any]) -> dict[str, Any]:
        joined = '\n'.join(line for line in lines if line)
        normalized_joined = normalize_text(joined).casefold()
        contact = self._required_contact(context)
        missing_required = []
        for required in (str(contact['name']), str(contact['email']), str(context['job']['company_name'])):
            normalized_required = normalize_text(required).casefold()
            if normalized_required and normalized_required not in normalized_joined:
                missing_required.append(required)
        contains_placeholder = '{{' in joined or 'TODO' in joined or '{job_title}' in joined or '{company_name}' in joined or '{name}' in joined
        valid = not contains_placeholder and not missing_required and len(joined.strip()) > 0
        failure_reason = None
        if contains_placeholder:
            failure_reason = 'cover_letter_contains_placeholder'
        elif missing_required:
            failure_reason = 'cover_letter_missing_required_fields'
        elif not joined.strip():
            failure_reason = 'cover_letter_empty'
        return {'contains_placeholder': contains_placeholder, 'missing_required_fields': missing_required, 'selected_fact_ids': context.get('selected_fact_ids', []), 'text_length': len(joined), 'valid': valid, 'failure_reason': failure_reason}


    def build_cover_letter_payload(self, context: dict[str, Any]) -> dict[str, Any]:
        contact = self._required_contact(context)
        draft = context.get('artifact_draft') or {}
        cover_draft = draft.get('cover_letter_draft') or context.get('cover_letter_draft') or {}
        if cover_draft.get('paragraphs'):
            return {
                'source': 'draft',
                'salutation': str(cover_draft.get('salutation') or f"Dear Hiring Team at {context['job']['company_name']},"),
                'paragraphs': [str(item).strip() for item in cover_draft.get('paragraphs', []) if str(item).strip()],
                'closing': str(cover_draft.get('closing') or 'Sincerely,'),
                'signature_name': str(cover_draft.get('signature_name') or contact['name']),
            }
        template_path = self.template_config.cover_letter_template_path
        if template_path is not None and template_path.exists():
            payload = json.loads(template_path.read_text(encoding='utf-8'))
            values = self._cover_letter_template_values(context)
            paragraphs = [self._render_template_text(str(item), values) for item in payload.get('paragraphs', [])]
            return {
                'source': 'local_template',
                'salutation': self._render_template_text(str(payload.get('salutation') or 'Dear Hiring Team at {company_name},'), values),
                'paragraphs': [paragraph for paragraph in paragraphs if paragraph],
                'closing': self._render_template_text(str(payload.get('closing') or 'Sincerely,'), values),
                'signature_name': self._render_template_text(str(payload.get('signature_name') or '{name}'), values),
            }
        work = context['profile'].get('work', [])
        projects = context['profile'].get('projects', [])
        paragraphs = [f"I am applying for the {context['job']['title']} role at {context['job']['company_name']}."]
        for entry in (work + projects)[:3]:
            summary = normalize_text(str(entry.get('summary') or entry.get('description') or entry.get('achievement') or ''))
            if summary:
                paragraphs.append(summary)
        paragraphs.append('Thank you for your consideration.')
        return {'source': 'generated', 'salutation': f"Dear Hiring Team at {context['job']['company_name']},", 'paragraphs': paragraphs, 'closing': 'Sincerely,', 'signature_name': str(contact['name'])}

    @staticmethod
    def _strip_markdown_code_fences(text: str) -> str:
        cleaned = str(text or '').strip()
        fence_match = re.search(r'^```(?:latex)?\s*([\s\S]*?)```$', cleaned, re.IGNORECASE)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        else:
            cleaned = re.sub(r'^```(?:latex)?\s*', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'\s*```$', '', cleaned)
        return cleaned.strip()

    @staticmethod
    def _latex_preamble_signature(source: str) -> list[str]:
        preamble, _, _body = str(source or "").partition("\\begin{document}")
        signature: list[str] = []
        blank_pending = False
        for line in preamble.splitlines():
            stripped = line.rstrip()
            normalized = " ".join(stripped.split()) if stripped.strip() else ""
            if not normalized:
                if signature and not blank_pending:
                    signature.append("")
                    blank_pending = True
                continue
            signature.append(normalized)
            blank_pending = False
        while signature and signature[-1] == "":
            signature.pop()
        return signature

    def _validate_generated_latex(self, generated: str, template_latex: str) -> str:
        cleaned = self._strip_markdown_code_fences(generated)
        if not cleaned.lstrip().startswith('\\documentclass'):
            raise ValueError('Generated LaTeX did not start with \\documentclass.')
        if not cleaned.rstrip().endswith('\\end{document}'):
            raise ValueError('Generated LaTeX did not end with \\end{document}.')
        if self._latex_preamble_signature(cleaned) != self._latex_preamble_signature(template_latex):
            raise ValueError('Generated LaTeX changed the template preamble.')
        return cleaned.rstrip() + '\n'

    @staticmethod
    def _latex_compile_feedback(validation: dict[str, Any]) -> str:
        detail = str(validation.get('stderr') or validation.get('stdout') or '').strip()
        if not detail:
            detail = 'Unknown LaTeX compilation error.'
        return (
            "Your previous LaTeX failed to compile with this error:\n"
            f"{detail[-4000:]}\n\n"
            "Fix the LaTeX and return the corrected version."
        )

    @staticmethod
    def _latex_structure_feedback(detail: str) -> str:
        message = str(detail or "Generated LaTeX changed protected template structure.").strip()
        return (
            "Your previous LaTeX changed protected template structure or returned malformed LaTeX.\n"
            f"{message}\n\n"
            "Keep the preamble before \\begin{document} identical to the source template.\n"
            "Preserve protected commands, macros, package imports, and layout definitions exactly.\n"
            "Return only the corrected full LaTeX."
        )

    def _one_page_overflow_feedback(
        self,
        output_path: Path,
        artifact_kind: str,
        *,
        validation: dict[str, Any] | None = None,
    ) -> str:
        details = validation or {}
        page_count = int(details.get('page_count') or self._pdf_page_count(output_path) or 2)
        overflow_line_count = int(details.get('overflow_line_count') or self._pdf_overflow_line_count(output_path) or 0)
        # Use 40-50% more than the actual overflow to ensure it fits on one page
        # e.g. 3 overflow lines -> reduce by 5 lines; 6 overflow lines -> reduce by 9
        if overflow_line_count > 0:
            buffer_multiplier = 1.5  # 50% buffer over the overflow
            suggested_trim = max(3, int(overflow_line_count * buffer_multiplier) + 1)
        else:
            suggested_trim = 5
        artifact_label = "resume" if artifact_kind == "resume" else "cover letter"
        return (
            f"/no-think\n"
            f"Your previous {artifact_label} compiled to {page_count} pages instead of one.\n"
            f"It exceeded one page by about {overflow_line_count or 'a few'} lines.\n"
            f"You MUST reduce the content by approximately {suggested_trim} lines so it fits exactly on one page.\n"
            "Strategies: shorten weaker bullet points, compress verbose phrasing, remove the least relevant bullet "
            "from entries that have many, tighten the summary. Do NOT remove entire sections or entries.\n"
            "Do NOT over-trim - the resume should still look full and professional, just shorter.\n"
            "Keep the same LaTeX template structure, macros, and formatting.\n"
            "Do not invent new claims.\n"
            "Return only the corrected LaTeX."
        )

    @staticmethod
    def _latex_direct_candidate_snapshot(context: dict[str, Any]) -> str:
        profile = context.get("profile") or {}
        job = context.get("job") or {}
        contact_entries = list(profile.get("contact") or [])
        contact = contact_entries[0] if contact_entries else {}

        def _clean(value: Any) -> str:
            return _normalize_render_text(value)

        def _list_items(value: Any, *, limit: int) -> list[str]:
            if limit <= 0:
                return []
            if isinstance(value, str):
                raw_items = [value]
            else:
                raw_items = list(value or [])
            cleaned: list[str] = []
            for item in raw_items:
                text = _clean(item)
                if text:
                    cleaned.append(text)
                if len(cleaned) >= limit:
                    break
            return cleaned

        def _compact_entries(entries: list[dict[str, Any]], *, limit: int, bullet_limit: int = 2) -> list[dict[str, Any]]:
            compacted: list[dict[str, Any]] = []
            for entry in list(entries or [])[:limit]:
                compact: dict[str, Any] = {}
                for key in ("title", "company", "name", "school", "degree", "minor", "location", "dates", "date_label", "summary", "description"):
                    value = _clean(entry.get(key))
                    if value:
                        compact[key] = value
                bullets = _list_items(entry.get("bullets") or entry.get("highlights"), limit=bullet_limit)
                if bullets:
                    compact["bullets"] = bullets
                technologies = _list_items(entry.get("technologies") or entry.get("skills"), limit=6)
                if technologies:
                    compact["technologies"] = technologies
                if compact:
                    compacted.append(compact)
            return compacted

        skill_names: list[str] = []
        for entry in list(profile.get("skills") or [])[:14]:
            name = _clean(entry.get("name") or entry.get("skill"))
            if name and name not in skill_names:
                skill_names.append(name)

        snapshot = {
            "target_job": {
                "company": _clean(job.get("company_name")),
                "title": _clean(job.get("title")),
                "location": _clean(job.get("location_raw")),
            },
            "candidate": {
                "name": _clean(contact.get("name")),
                "email": _clean(contact.get("email")),
                "phone": _clean(contact.get("phone")),
                "links": _list_items(
                    [
                        contact.get("linkedin"),
                        contact.get("github"),
                        contact.get("portfolio"),
                        contact.get("website"),
                    ],
                    limit=4,
                ),
            },
            "education": _compact_entries(list(profile.get("education") or []), limit=2, bullet_limit=0),
            "experience": _compact_entries(list(profile.get("work") or []), limit=4),
            "projects": _compact_entries(list(profile.get("projects") or []), limit=3),
            "skills": skill_names,
            "selected_fact_ids": list(context.get("selected_fact_ids") or []),
        }
        return json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True)

    async def generate_latex_resume(
        self,
        template_latex: str,
        job_description: str,
        model_router,
        role,
        *,
        repair_feedback: str | None = None,
    ) -> str:
        context = self._raw_latex_validation_context or {}
        sanitized_job_description = normalize_text(strip_html_tags(job_description or ''))
        target_job = context.get("job") or {}
        company = _normalize_render_text(target_job.get("company_name") or "the company")
        role_label = _normalize_render_text(target_job.get("title") or "the target role")
        candidate_snapshot = self._latex_direct_candidate_snapshot(context)
        system_prompt = (
            "You are an expert ATS resume editor. You receive a LaTeX resume and a job description. "
            "You return ONLY the edited LaTeX code. No explanation, no commentary, no markdown fences. "
            "Just the raw LaTeX starting with \\documentclass and ending with \\end{document}."
        )
        prompt = (
            f"/no-think edit this resume to fit the attached job description. "
            f"Make sure it is a perfect ATS resume and make sure you dont change the resume format "
            f"just make it appropriate for the job. Just return me the edited latex code.\n\n"
            f"TARGET ROLE: {role_label} at {company}\n\n"
            "RULES:\n"
            "1. REWRITE the professional summary paragraph to directly target this role. "
            "Mention the company name, the role focus, and the candidate's strongest matching skills/results. "
            "This is NOT a generic summary - it must read as written FOR this specific job.\n"
            "2. REORDER the Technical Skills section: put skills mentioned in the job description FIRST. "
            "Add relevant skills from the candidate's background that match the job. Drop irrelevant ones.\n"
            "3. REWRITE experience bullet points to emphasize the aspects that match this job. "
            "Use keywords and terminology from the job posting where they truthfully match real experience. "
            "Put the strongest matching bullet first in each role.\n"
            "4. REWRITE project descriptions the same way - lead with what matches the job requirements.\n"
            "5. If the job mentions specific technologies, methodologies, or domains that the candidate "
            "genuinely has experience with, make sure those appear prominently.\n"
            "6. The resume must fit on EXACTLY ONE PAGE. Do not exceed one page.\n\n"
            "DO NOT:\n"
            "- Do NOT change the LaTeX preamble (everything before \\begin{document}). Keep all packages, "
            "macros, spacing, margins, and formatting commands identical.\n"
            "- Do NOT change the candidate's name, contact info, email, phone, links.\n"
            "- Do NOT change education details (school, degree, dates, GPA).\n"
            "- Do NOT change employer names or employment dates.\n"
            "- Do NOT invent skills, technologies, metrics, employers, or certifications.\n"
            "- Do NOT copy sentences from the job description verbatim.\n"
            "- Do NOT add any text outside the LaTeX code.\n\n"
            "CANDIDATE FACTS (source of truth for what is real):\n"
            f"{candidate_snapshot}\n\n"
            "latex code:\n"
            f"{template_latex}\n\n"
            "job description:\n"
            f"{sanitized_job_description}"
        )
        if repair_feedback:
            prompt = f"{prompt}\n\n{repair_feedback}"
        response = await model_router.generate_text(role, prompt, system_prompt=system_prompt)
        return self._validate_generated_latex(response, template_latex)

    async def generate_latex_cover_letter(
        self,
        template_latex: str,
        job_description: str,
        company_name: str,
        job_title: str,
        model_router,
        role,
        *,
        repair_feedback: str | None = None,
    ) -> str:
        sanitized_job_description = normalize_text(strip_html_tags(job_description or ''))
        today_label = datetime.now().strftime('%d %B %Y')
        company = str(company_name or 'the company').strip() or 'the company'
        title = str(job_title or 'the role').strip() or 'the role'
        context = self._raw_latex_validation_context or {}
        candidate_snapshot = self._latex_direct_candidate_snapshot(context)
        system_prompt = (
            "You are an expert cover letter writer. You receive a LaTeX cover letter template and a job description. "
            "You return ONLY the edited LaTeX code. No explanation, no commentary, no markdown fences. "
            "Just the raw LaTeX starting with \\documentclass and ending with \\end{document}."
        )
        prompt = (
            f"/no-think edit this cover letter to fit the attached job description. "
            f"Make sure it is a perfect cover letter for this role and make sure you dont change the cover letter format "
            f"just make it appropriate for the job. Just return me the edited latex code.\n\n"
            f"TARGET ROLE: {title} at {company}\n"
            f"DATE: {today_label}\n\n"
            "RULES:\n"
            "1. UPDATE the recipient line to: \"Dear {company} Hiring Team,\" or \"Dear Hiring Manager,\"\n"
            f"2. UPDATE the subject line to: \"Application for the {title} position\"\n"
            f"3. UPDATE the date to: {today_label}\n"
            "4. COMPLETELY REWRITE all body paragraphs. Write EXACTLY 3 body paragraphs in FIRST PERSON (I/my/me):\n"
            f"   - Paragraph 1: Why you are excited about THIS specific role at {company}. "
            "Be specific about what draws you to the company and role. NO generic openers like "
            "'I am writing to express my interest'.\n"
            "   - Paragraph 2: Your STRONGEST evidence of fit. Name specific projects, skills, "
            "and measurable results that directly match what the job posting asks for. "
            "Be concrete with numbers and technologies.\n"
            "   - Paragraph 3: What you would specifically contribute in this role, "
            "enthusiasm for the company mission, and a confident closing.\n"
            "5. Keep it to ONE PAGE. Every sentence must add value.\n\n"
            "DO NOT:\n"
            "- Do NOT change the LaTeX preamble (packages, margins, fonts, spacing commands).\n"
            "- Do NOT change the sender contact info (name, address, phone, email).\n"
            "- Do NOT invent experience, projects, metrics, or technologies.\n"
            "- Do NOT copy sentences from the job description.\n"
            "- Do NOT use third person or refer to yourself by name in the body.\n"
            "- Do NOT write self-deprecating language about gaps.\n"
            "- Do NOT add any text outside the LaTeX code.\n\n"
            "CANDIDATE FACTS (source of truth for what is real):\n"
            f"{candidate_snapshot}\n\n"
            "latex code:\n"
            f"{template_latex}\n\n"
            "job description:\n"
            f"Company: {company}\n"
            f"Position: {title}\n"
            f"{sanitized_job_description}"
        )
        if repair_feedback:
            prompt = f"{prompt}\n\n{repair_feedback}"
        response = await model_router.generate_text(role, prompt, system_prompt=system_prompt)
        return self._validate_generated_latex(response, template_latex)

    def compile_raw_latex(self, latex_source: str, base_name: str, artifact_kind: str) -> RenderedArtifact:
        output_path = self.artifacts_dir / f'{base_name}.{artifact_kind}.pdf'
        engine = find_latex_engine()
        selected_fact_ids = list((self._raw_latex_validation_context or {}).get('selected_fact_ids', []))
        if not engine:
            return RenderedArtifact(
                kind='pdf',
                path=output_path,
                content_hash='',
                validation_results={'valid': False, 'failure_reason': 'latex_not_installed', 'selected_fact_ids': selected_fact_ids},
            )

        build_dir = self.artifacts_dir / f'{base_name}.latex_direct'
        shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True, exist_ok=True)

        source_path = build_dir / f'{artifact_kind}.tex'
        source_path.write_text(latex_source, encoding='utf-8')

        command = [
            engine,
            '-interaction=nonstopmode',
            '-halt-on-error',
            '-file-line-error',
            '-output-directory',
            str(build_dir),
            str(source_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=self._latex_subprocess_env(),
        )

        candidate_pdf = build_dir / f'{artifact_kind}.pdf'
        validation = {
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
            'selected_fact_ids': selected_fact_ids,
        }
        if completed.returncode != 0 or not candidate_pdf.exists():
            validation['valid'] = False
            validation['failure_reason'] = 'latex_compile_failed'
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results=validation)

        shutil.copyfile(candidate_pdf, output_path)
        context = self._raw_latex_validation_context or {'job': {'company_name': ''}, 'profile': {'contact': [{'name': '', 'email': ''}]}}
        validation.update(self.validate_pdf(output_path, expect_one_page=True, context=context))
        return RenderedArtifact(kind='pdf', path=output_path, content_hash=hash_content(output_path.read_bytes().hex()), validation_results=validation)

    async def _generate_latex_with_retry(
        self,
        generate_func,
        *args,
        base_name: str,
        artifact_kind: str,
        validation_context: dict[str, Any],
    ) -> tuple[str, RenderedArtifact]:
        previous_context = self._raw_latex_validation_context
        self._raw_latex_validation_context = validation_context
        try:
            repair_feedback: str | None = None
            for attempt in range(3):
                try:
                    latex_source = await generate_func(*args, repair_feedback=repair_feedback)
                except ValueError as exc:
                    if attempt == 2:
                        raise
                    repair_feedback = self._latex_structure_feedback(str(exc))
                    continue
                artifact = self.compile_raw_latex(latex_source, base_name, artifact_kind)
                if artifact.validation_results.get('valid'):
                    return latex_source, artifact
                reason = str(artifact.validation_results.get('failure_reason') or f'invalid_{artifact_kind}')
                if reason == 'latex_compile_failed':
                    next_feedback = self._latex_compile_feedback(artifact.validation_results)
                elif reason == 'resume_exceeds_one_page':
                    next_feedback = self._one_page_overflow_feedback(
                        artifact.path,
                        artifact_kind,
                        validation=artifact.validation_results,
                    )
                else:
                    raise ValueError(f'{artifact.path.name}: {reason}')
                if attempt == 2:
                    raise ValueError(f'{artifact.path.name}: {reason}')
                repair_feedback = next_feedback
        finally:
            self._raw_latex_validation_context = previous_context
        raise ValueError(f'{artifact_kind}.pdf: latex_compile_failed')

    async def build_application_artifacts_latex_direct(
        self,
        job: NormalizedJobPosting,
        facts: list[ProfileFact],
        model_router,
        *,
        resume_template_path: Path,
        cover_letter_template_path: Path,
    ) -> list[RenderedArtifact]:
        from findmyjob.core.enums import ModelRole

        base_name = self.deterministic_base_name(job)
        job_description = normalize_text(strip_html_tags(job.description or job.normalized_description or ''))
        context = self.build_resume_context(job, facts)
        self.write_context(base_name, context)

        resume_template = resume_template_path.read_text(encoding='utf-8-sig')
        resume_latex, resume_pdf = await self._generate_latex_with_retry(
            self.generate_latex_resume,
            resume_template,
            job_description,
            model_router,
            ModelRole.RESUME_WRITER,
            base_name=base_name,
            artifact_kind='resume',
            validation_context=context,
        )

        cover_template = cover_letter_template_path.read_text(encoding='utf-8-sig')
        cover_letter_latex, cover_letter_pdf = await self._generate_latex_with_retry(
            self.generate_latex_cover_letter,
            cover_template,
            job_description,
            job.company_name,
            job.title,
            model_router,
            ModelRole.COVER_LETTER_WRITER,
            base_name=base_name,
            artifact_kind='cover_letter',
            validation_context=context,
        )

        resume_text = self.write_resume_text_from_pdf(base_name, resume_pdf, context)
        cover_letter_text = self.write_cover_letter_text_from_pdf(base_name, cover_letter_pdf, context)

        latex_resume_path = self.artifacts_dir / f'{base_name}.resume.tex'
        latex_resume_path.write_text(resume_latex, encoding='utf-8')
        latex_cover_letter_path = self.artifacts_dir / f'{base_name}.cover_letter.tex'
        latex_cover_letter_path.write_text(cover_letter_latex, encoding='utf-8')

        artifacts = [resume_pdf, cover_letter_pdf, resume_text, cover_letter_text]
        failures = self.validation_failures(
            artifacts,
            expected_suffixes={'resume.txt', 'cover_letter.txt', 'resume.pdf', 'cover_letter.pdf'},
            blocking_only=True,
        )
        if failures:
            raise ValueError('; '.join(failures))
        return artifacts

    def build_application_artifacts(self, job: NormalizedJobPosting, facts: list[ProfileFact], artifact_draft: ArtifactDraft | None = None) -> list[RenderedArtifact]:
        base_name = self.deterministic_base_name(job)
        context = self.build_resume_context(job, facts, artifact_draft=artifact_draft)
        if artifact_draft is not None:
            context['artifact_draft'] = artifact_draft.model_dump(mode='json')
            context['cover_letter_draft'] = artifact_draft.cover_letter_draft.model_dump(mode='json')
        context['cover_letter'] = self.build_cover_letter_payload(context)
        artifacts: list[RenderedArtifact] = [self.write_context(base_name, context)]
        if self.resume_renderer == 'latex' and self.template_config.resume_template_path is not None:
            resume_pdf = self.render_latex_resume(base_name, context)
            resume_text = self.write_resume_text_from_pdf(base_name, resume_pdf, context)
        else:
            resume_text = self.write_resume_text(base_name, context)
            resume_pdf = self.render_typst('resume.typ', base_name, context)
        cover_letter_text = self.write_cover_letter_text(base_name, context)
        if self.resume_renderer == 'latex':
            cover_letter_pdf = self.render_latex_cover_letter(base_name, context)
        else:
            cover_letter_pdf = self.render_typst('cover_letter.typ', base_name, context)
        artifacts.extend([resume_text, cover_letter_text, resume_pdf, cover_letter_pdf])
        failures = self.validation_failures(artifacts, blocking_only=True)
        if failures:
            raise ValueError('; '.join(failures))
        return artifacts

    def validation_failures(
        self,
        artifacts: list[RenderedArtifact],
        *,
        expected_suffixes: set[str] | None = None,
        blocking_only: bool = True,
    ) -> list[str]:
        failures: list[str] = []
        for artifact in artifacts:
            validation = artifact.validation_results or {}
            if validation.get('valid', True):
                continue
            reason = validation.get('failure_reason') or f'invalid_{artifact.kind}'
            if blocking_only and not self._is_blocking_validation_reason(str(reason)):
                continue
            failures.append(f'{artifact.path.name}: {reason}')
        required_suffixes = expected_suffixes or {'resume.txt', 'cover_letter.txt', 'resume.pdf', 'cover_letter.pdf'}
        actual_suffixes = {artifact.path.name.split('.', 1)[-1] if '.' in artifact.path.name else artifact.path.name for artifact in artifacts}
        for suffix in sorted(required_suffixes - actual_suffixes):
            failures.append(f'missing_artifact:{suffix}')
        return failures

    def validation_warnings(
        self,
        artifacts: list[RenderedArtifact],
    ) -> list[str]:
        warnings: list[str] = []
        for artifact in artifacts:
            validation = artifact.validation_results or {}
            if validation.get('valid', True):
                continue
            reason = validation.get('failure_reason') or f'invalid_{artifact.kind}'
            if self._is_blocking_validation_reason(str(reason)):
                continue
            warnings.append(f'{artifact.path.name}: {reason}')
        return warnings

    def _is_blocking_validation_reason(self, reason: str) -> bool:
        blocking_exceptions = {
            'plain_text_contains_unsupported_lines',
            'cover_letter_missing_required_fields',
            'resume_text_missing_contact_fields',
            'resume_text_empty',
        }
        return str(reason or '').strip() not in blocking_exceptions

    def _job_terms(self, job: NormalizedJobPosting) -> set[str]:
        haystack = f'{job.title} {job.normalized_description}'.lower()
        return {token for token in haystack.split() if len(token) > 2}

    def _score_payload(self, payload: dict[str, Any], job_terms: set[str]) -> int:
        text = ' '.join(str(payload.get(field, '')) for field in ('summary', 'description', 'achievement', 'title', 'name', 'skills'))
        payload_terms = {token for token in normalize_text(text).lower().split() if len(token) > 2}
        return len(payload_terms & job_terms)

    def _shrink_context_until_one_page(self, output_path: Path, context: dict[str, Any], base_name: str, template_name: str, typst: str) -> dict[str, Any]:
        current = json.loads(json.dumps(context))
        while self._pdf_page_count(output_path) > 1:
            if current['profile'].get('projects'):
                current['profile']['projects'] = current['profile']['projects'][:-1]
            elif current['profile'].get('work') and len(current['profile']['work']) > 1:
                current['profile']['work'] = current['profile']['work'][:-1]
            elif current['profile'].get('skills') and len(current['profile']['skills']) > 4:
                current['profile']['skills'] = current['profile']['skills'][:-1]
            else:
                break
            context_path = self.artifacts_dir / f'{base_name}.render.json'
            context_path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding='utf-8')
            template_path = self.template_dir / template_name
            command = [
                typst,
                'compile',
                '--root',
                str(self._typst_project_root(template_path, context_path, output_path)),
                str(template_path),
                str(output_path),
                f'--input=context={self._typst_context_argument(template_path, context_path)}',
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                break
        return current

    def _pdf_page_count(self, output_path: Path) -> int:
        try:
            return len(PdfReader(str(output_path)).pages)
        except Exception:
            return 99

    def _pdf_overflow_line_count(self, output_path: Path) -> int:
        try:
            reader = PdfReader(str(output_path))
            if len(reader.pages) <= 1:
                return 0
            overflow_text = '\n'.join(reader.pages[index].extract_text() or '' for index in range(1, len(reader.pages)))
        except Exception:
            return 0
        return len([line for line in overflow_text.splitlines() if line.strip()])

    def _validate_required_contact(self, contact_entries: list[dict[str, Any]]) -> None:
        if not contact_entries:
            raise ValueError('Missing required contact facts: name, email')
        contact = contact_entries[0]
        missing = [field for field in ('name', 'email') if not str(contact.get(field, '')).strip()]
        if missing:
            raise ValueError(f"Missing required contact facts: {', '.join(missing)}")

    def _required_contact(self, context: dict[str, Any]) -> dict[str, Any]:
        contact_entries = context['profile'].get('contact', [])
        self._validate_required_contact(contact_entries)
        return contact_entries[0]

    def _cover_letter_template_values(self, context: dict[str, Any]) -> dict[str, str]:
        contact = self._required_contact(context)
        education = context['profile'].get('education', [])
        first_education = education[0] if education else {}
        return {
            'company_name': str(context['job']['company_name']),
            'job_title': str(context['job']['title']),
            'name': str(contact['name']),
            'email': str(contact['email']),
            'phone': str(contact.get('phone') or ''),
            'linkedin': str(contact.get('linkedin') or ''),
            'portfolio': str(contact.get('portfolio') or contact.get('website') or ''),
            'school': str(first_education.get('school') or ''),
            'degree': str(first_education.get('degree') or ''),
        }

    def _render_template_text(self, value: str, template_values: dict[str, str]) -> str:
        class _SafeDict(dict):
            def __missing__(self, key: str) -> str:
                return '{' + key + '}'

        return value.format_map(_SafeDict(template_values)).strip()

    def _typst_context_argument(self, template_path: Path, context_path: Path) -> str:
        try:
            relative = os.path.relpath(
                context_path.resolve(),
                start=template_path.parent.resolve(),
            )
        except ValueError:
            relative = context_path.name
        return Path(relative).as_posix()






    def _typst_project_root(self, template_path: Path, context_path: Path, output_path: Path) -> Path:
        return Path(
            os.path.commonpath(
                [
                    str(template_path.parent.resolve()),
                    str(context_path.parent.resolve()),
                    str(output_path.parent.resolve()),
                ]
            )
        )

    def _build_latex_resume_source(self, context: dict[str, Any]) -> str:
        """Build a LaTeX resume by cloning the user's actual CV template and
        surgically replacing the content sections (Research Summary, Research
        Experience, Experience, Selected Projects, Skills) while preserving
        the original preamble, macros, header, contact info, and education."""
        import re as _re

        template_path = self.template_config.resume_template_path
        if template_path is not None and template_path.exists():
            source = template_path.read_text(encoding='utf-8-sig')
        else:
            # Fallback: build a minimal document if template is missing
            return self._build_fallback_latex_resume(context)

        work = context['profile'].get('work', [])
        projects = context['profile'].get('projects', [])
        skills = context['profile'].get('skills', [])
        draft = context.get('resume_draft') or {}
        job = context.get('job') or {}

        def esc(value: Any) -> str:
            return _latex_escape(value)

        # --- Build Research Summary tailored to the target role ---
        summary_lines = [esc(line) for line in draft.get('summary_lines', []) if str(line).strip()]
        if summary_lines:
            summary_text = ' '.join(summary_lines[:3])
        else:
            summary_text = (
                f"Early-career software engineer focused on reliable systems, practical automation, and clear operator tooling. "
                f"Seeking {esc(job.get('title', 'software engineering'))} roles where I can build maintainable products, "
                f"improve workflow visibility, and ship evidence-backed improvements."
            )
        def _replace_summary(match: _re.Match) -> str:
            return match.group(1) + summary_text + match.group(2)
        source = _re.sub(
            r'(\\section\{Research Summary\}\s*\{\\small\s*).*?(\s*\})',
            _replace_summary,
            source,
            flags=_re.DOTALL,
        )

        # --- Classify work facts into research vs industry ---
        _research_keywords = {'research', 'researcher', 'intern', 'lab', 'laboratory', 'cubesat', 'satellite', 'phd', 'graduate'}

        def _is_research_entry(entry: dict[str, Any]) -> bool:
            text = ' '.join(str(entry.get(k, '')) for k in ('title', 'company', 'summary', 'description')).casefold()
            return bool(_research_keywords & set(text.split()))

        research_work = [e for e in work if _is_research_entry(e)]
        industry_work = [e for e in work if not _is_research_entry(e)]

        def _build_work_entries(entries: list[dict[str, Any]], limit: int) -> list[str]:
            built: list[str] = []
            for entry in entries[:limit]:
                company = esc(entry.get('company', ''))
                title_text = esc(entry.get('title', ''))
                dates = esc(entry.get('dates', ''))
                location = esc(entry.get('location', ''))
                summary = entry.get('summary') or entry.get('description') or entry.get('achievement') or ''
                bullets_raw = [line.strip() for line in str(summary).split('\n') if line.strip()]
                if not bullets_raw:
                    bullets_raw = [str(summary)] if summary else []
                entry_lines = [f'  \\resumeSubheading{{{title_text}}}{{{dates}}}{{{company}}}{{{location}}}']
                if bullets_raw:
                    entry_lines.append('  \\resumeItemListStart')
                    for bullet in bullets_raw[:5]:
                        entry_lines.append(f'    \\resumeItem{{{esc(bullet)}}}')
                    entry_lines.append('  \\resumeItemListEnd')
                built.append('\n'.join(entry_lines))
            return built

        # --- Build Research Experience section from research work facts ---
        if research_work and _re.search(r'\\section\{Research Experience\}', source):
            research_entries = _build_work_entries(research_work, 3)
            new_research = (
                '\\section{Research Experience}\n'
                '\\resumeSubHeadingListStart\n\n'
                + '\n\n'.join(research_entries) + '\n\n'
                '\\resumeSubHeadingListEnd'
            )
            source = _re.sub(
                r'\\section\{Research Experience\}.*?\\resumeSubHeadingListEnd',
                lambda _: new_research,
                source,
                flags=_re.DOTALL,
            )

        # --- Build Experience section from industry work facts ---
        experience_pool = industry_work if industry_work else work
        if experience_pool:
            experience_entries = _build_work_entries(experience_pool, 3)
            # Add custom bullets from the AI draft
            for bullet in draft.get('custom_bullets', [])[:3]:
                if str(bullet).strip():
                    experience_entries.append(f'    \\resumeItem{{{esc(bullet)}}}')
            new_experience = (
                '\\section{Experience}\n'
                '\\resumeSubHeadingListStart\n\n'
                + '\n\n'.join(experience_entries) + '\n\n'
                '\\resumeSubHeadingListEnd'
            )
            source = _re.sub(
                r'\\section\{Experience\}.*?\\resumeSubHeadingListEnd',
                lambda _: new_experience,
                source,
                flags=_re.DOTALL,
            )

        # --- Build Selected Projects section ---
        if projects:
            project_entries = []
            for entry in projects[:3]:
                name = esc(entry.get('name') or entry.get('title') or 'Project')
                dates = esc(entry.get('dates', ''))
                tech = esc(entry.get('tech', '') or entry.get('summary', '')[:80])
                summary = entry.get('summary') or entry.get('description') or entry.get('achievement') or ''
                bullets_raw = [line.strip() for line in str(summary).split('\n') if line.strip()]
                if not bullets_raw:
                    bullets_raw = [str(summary)] if summary else []
                entry_lines = [f'  \\resumeProject{{{name}}}{{{dates}}}{{{tech}}}']
                if bullets_raw:
                    entry_lines.append('  \\resumeItemListStart')
                    for bullet in bullets_raw[:3]:
                        entry_lines.append(f'    \\resumeItem{{{esc(bullet)}}}')
                    entry_lines.append('  \\resumeItemListEnd')
                project_entries.append('\n'.join(entry_lines))
            new_projects = (
                '\\section{Selected Projects}\n'
                '\\resumeSubHeadingListStart\n\n'
                + '\n\n'.join(project_entries) + '\n\n'
                '\\resumeSubHeadingListEnd'
            )
            source = _re.sub(
                r'\\section\{Selected Projects\}.*?\\resumeSubHeadingListEnd',
                lambda _: new_projects,
                source,
                flags=_re.DOTALL,
            )

        # --- Build Skills section ---
        if skills:
            skill_names = [esc(entry.get('name', '')) for entry in skills if entry.get('name')]
            if skill_names:
                skill_categories = {}
                for entry in skills:
                    cat = entry.get('category', 'General')
                    name = entry.get('name', '')
                    if name:
                        skill_categories.setdefault(cat, []).append(esc(name))
                if len(skill_categories) > 1:
                    skill_lines = []
                    for cat, names in skill_categories.items():
                        skill_lines.append('\\textbf{' + esc(cat) + ':} ' + ', '.join(names))
                    new_skills = '\\section{Skills}\n{\\footnotesize\n' + ' \\\\\n'.join(skill_lines) + '\n}'
                else:
                    new_skills = '\\section{Skills}\n{\\footnotesize\n' + ', '.join(skill_names[:20]) + '\n}'
                new_skills_with_spacing = new_skills + '\n\n'
                source = _re.sub(
                    r'\\section\{Skills\}.*?(?=\\section|\\end\{document\})',
                    lambda _: new_skills_with_spacing,
                    source,
                    flags=_re.DOTALL,
                )

        return source

    def _build_fallback_latex_resume(self, context: dict[str, Any]) -> str:
        """Minimal LaTeX resume when no user template is available."""
        contact = self._required_contact(context)
        work = context['profile'].get('work', [])
        projects = context['profile'].get('projects', [])
        skills = context['profile'].get('skills', [])

        def esc(value: Any) -> str:
            return _latex_escape(value)

        lines = [
            r'\documentclass[letterpaper,10pt]{article}',
            r'\usepackage[margin=0.6in]{geometry}',
            r'\usepackage[T1]{fontenc}',
            r'\usepackage[utf8]{inputenc}',
            r'\usepackage[hidelinks]{hyperref}',
            r'\usepackage{enumitem}',
            r'\setlist[itemize]{leftmargin=1.1em,itemsep=1pt,topsep=2pt}',
            r'\pagestyle{empty}',
            r'\begin{document}',
            r'\begin{center}',
            f"{{\\Large \\textbf{{{esc(contact.get('name'))}}}}}\\\\",
            esc(contact.get('email')),
        ]
        optional_contact = [contact.get('phone'), contact.get('linkedin') or contact.get('portfolio') or contact.get('website')]
        optional_contact = [esc(value) for value in optional_contact if value]
        if optional_contact:
            lines[-1] += ' | ' + ' | '.join(optional_contact)
        lines.append(r'\end{center}')
        if work:
            lines.extend([r'\section*{Experience}', r'\begin{itemize}'])
            for entry in work[:3]:
                title = ' - '.join(item for item in [esc(entry.get('company')), esc(entry.get('title'))] if item)
                summary = esc(entry.get('summary') or entry.get('description') or '')
                if title:
                    lines.append(f'\\item {title}')
                if summary:
                    lines.append(f'\\item {summary}')
            lines.append(r'\end{itemize}')
        if projects:
            lines.extend([r'\section*{Projects}', r'\begin{itemize}'])
            for entry in projects[:2]:
                name = esc(entry.get('name') or entry.get('title') or 'Project')
                summary = esc(entry.get('summary') or entry.get('description') or '')
                lines.append(f'\\item {name}: {summary}' if summary else f'\\item {name}')
            lines.append(r'\end{itemize}')
        if skills:
            skill_names = [esc(entry.get('name')) for entry in skills if entry.get('name')]
            if skill_names:
                lines.extend([r'\section*{Skills}', ', '.join(skill_names[:16])])
        lines.append(r'\end{document}')
        return '\n'.join(lines) + '\n'

    def render_latex_cover_letter(self, base_name: str, context: dict[str, Any]) -> RenderedArtifact:
        """Compile a professional LaTeX cover letter to PDF."""
        output_path = self.artifacts_dir / f'{base_name}.cover_letter.pdf'
        engine = find_latex_engine()
        if not engine:
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results={'valid': False, 'failure_reason': 'latex_not_installed', 'selected_fact_ids': context.get('selected_fact_ids', [])})
        build_dir = self.artifacts_dir / f'{base_name}.latex'
        shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True, exist_ok=True)
        source_path = build_dir / 'cover_letter.tex'
        source_path.write_text(self._build_latex_cover_letter_source(context), encoding='utf-8')
        command = [engine, '-interaction=nonstopmode', '-halt-on-error', '-file-line-error', '-output-directory', str(build_dir), str(source_path)]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=self._latex_subprocess_env(),
        )
        candidate_pdf = build_dir / 'cover_letter.pdf'
        validation = {'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr, 'selected_fact_ids': context.get('selected_fact_ids', [])}
        if completed.returncode != 0 or not candidate_pdf.exists():
            validation['valid'] = False
            validation['failure_reason'] = 'latex_compile_failed'
            return RenderedArtifact(kind='pdf', path=output_path, content_hash='', validation_results=validation)
        shutil.copyfile(candidate_pdf, output_path)
        validation.update(self.validate_pdf(output_path, expect_one_page=True, context=context))
        return RenderedArtifact(kind='pdf', path=output_path, content_hash=hash_content(output_path.read_bytes().hex()), validation_results=validation)

    def _build_latex_cover_letter_source(self, context: dict[str, Any]) -> str:
        """Generate a LaTeX cover letter that matches the resume template styling."""
        contact = self._required_contact(context)
        cover_letter = context.get('cover_letter') or {}
        job = context.get('job') or {}

        def esc(value: Any) -> str:
            return _latex_escape(value)

        salutation = esc(cover_letter.get('salutation', f"Dear Hiring Team at {job.get('company_name', 'the Company')},")) 
        paragraphs = [esc(p) for p in cover_letter.get('paragraphs', []) if str(p).strip()]
        closing = esc(cover_letter.get('closing', 'Sincerely,'))
        sig_name = esc(cover_letter.get('signature_name') or contact.get('name', ''))
        education = list((context.get('profile') or {}).get('education') or [])
        first_education = education[0] if education else {}

        body_text = '\n\n\\vspace{10pt}\n'.join(paragraphs) if paragraphs else f'I am writing to express my strong interest in the {esc(job.get("title", "open"))} position at {esc(job.get("company_name", "your company"))}.'

        # Contact line items shown below the signature.
        contact_parts = []
        if contact.get('phone'):
            contact_parts.append(f'\\small {esc(contact.get("phone", ""))}')
        if contact.get('email'):
            contact_parts.append(f'\\href{{\\detokenize{{mailto:{_latex_href_target(contact["email"])}}}}}{{{esc(contact["email"])}}}')
        if contact.get('linkedin'):
            contact_parts.append(f'\\href{{\\detokenize{{{_latex_href_target(contact["linkedin"])}}}}}{{{esc(contact["linkedin"])}}}')
        if contact.get('github'):
            contact_parts.append(f'\\href{{\\detokenize{{{_latex_href_target(contact["github"])}}}}}{{{esc(contact["github"])}}}')
        if contact.get('portfolio') or contact.get('website'):
            url = contact.get('portfolio') or contact.get('website')
            contact_parts.append(f'\\href{{\\detokenize{{{_latex_href_target(url)}}}}}{{{esc(url)}}}')
        contact_line = ' \\,|\\, '.join(contact_parts)
        education_line = ', '.join(
            esc(part)
            for part in (
                first_education.get('school'),
                first_education.get('degree'),
                first_education.get('date_label') or first_education.get('dates'),
            )
            if str(part or '').strip()
        )
        signature_block = f'{sig_name}\n\n'
        if education_line:
            signature_block += f'{education_line}\n'
        if contact_line:
            signature_block += f'{contact_line}\n'

        return (
            '\\documentclass[letterpaper,10pt]{article}\n'
            '\\usepackage[utf8]{inputenc}\n'
            '\\usepackage[T1]{fontenc}\n'
            '\\usepackage{textcomp}\n'
            '\\usepackage{lmodern}\n'
            '\\usepackage{microtype}\n'
            '\\microtypesetup{expansion=false}\n'
            '\\usepackage[empty]{fullpage}\n'
            '\\usepackage[hidelinks]{hyperref}\n'
            '\\usepackage{fancyhdr}\n'
            '\\pagestyle{fancy}\n'
            '\\fancyhf{}\n'
            '\\fancyfoot{}\n'
            '\\renewcommand{\\headrulewidth}{0pt}\n'
            '\\renewcommand{\\footrulewidth}{0pt}\n'
            '\\setlength{\\footskip}{6pt}\n'
            '\n'
            '\\addtolength{\\oddsidemargin}{-0.5in}\n'
            '\\addtolength{\\evensidemargin}{-0.5in}\n'
            '\\addtolength{\\textwidth}{1.0in}\n'
            '\\addtolength{\\topmargin}{-0.66in}\n'
            '\\addtolength{\\textheight}{1.38in}\n'
            '\n'
            '\\urlstyle{same}\n'
            '\\raggedbottom\n'
            '\\setlength{\\parindent}{0pt}\n'
            '\\setlength{\\parskip}{0pt}\n'
            '\n'
            '\\begin{document}\n'
            '\n'
            f'{salutation}\n'
            '\n'
            '\\vspace{10pt}\n'
            f'{body_text}\n'
            '\n'
            '\\vspace{10pt}\n'
            f'{closing}\n'
            '\n'
            '\\vspace{16pt}\n'
            f'{signature_block}'
            '\n'
            '\\end{document}\n'
        )
