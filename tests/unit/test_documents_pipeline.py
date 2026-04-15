from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
import json
from pathlib import Path

import anyio
import pytest

from findmyjob.core.enums import FactKind, Sensitivity, WorkplaceType
from findmyjob.core.types import ArtifactDraft, CoverLetterDraft, NormalizedJobPosting, ProfileFact, ResumeDraft
from findmyjob.documents.pipeline import DocumentPipeline, DocumentTemplateConfig, RenderedArtifact
from findmyjob.filefirst.text_utils import drop_trailing_single_character_lines


@pytest.fixture()
def job() -> NormalizedJobPosting:
    return NormalizedJobPosting(
        company_name="Acme",
        company_key="acme",
        title="Software Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="123",
        posting_url="https://boards.greenhouse.io/acme/jobs/123",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        location_raw="Remote",
        location_normalized="remote",
        workplace_type=WorkplaceType.REMOTE,
        employment_type="full_time",
        compensation=None,
        description="Build reliable backend systems.",
        normalized_description="build reliable backend systems",
        discovered_at=datetime.now(timezone.utc),
        job_identity_key="identity-123",
        duplicate_cluster_key="cluster-123",
    )


@pytest.fixture()
def facts() -> list[ProfileFact]:
    return [
        ProfileFact(fact_id="contact-1", kind=FactKind.CONTACT, payload={"name": "Test User", "email": "user@example.com", "phone": "555-0100"}, sensitivity=Sensitivity.LOW),
        ProfileFact(fact_id="work-1", kind=FactKind.WORK, payload={"summary": "Built reliable backend systems."}, sensitivity=Sensitivity.LOW),
        ProfileFact(fact_id="project-1", kind=FactKind.PROJECT, payload={"summary": "Shipped a compliant job application tool."}, sensitivity=Sensitivity.LOW),
        ProfileFact(fact_id="skill-1", kind=FactKind.SKILL, payload={"name": "Python", "summary": "Python"}, sensitivity=Sensitivity.LOW),
    ]


def test_validate_plain_text_allows_contact_lines(tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    pipeline = DocumentPipeline(tmp_path / "artifacts", tmp_path / "templates")
    context = pipeline.build_resume_context(job, facts)
    validation = pipeline.validate_plain_text(
        [
            "Test User",
            "user@example.com",
            "555-0100",
            "Software Engineer",
            "Built reliable backend systems.",
            "Skills: Python",
        ],
        context,
    )
    assert validation["valid"] is True
    assert validation["unsupported_lines"] == []


def test_validate_plain_text_rejects_unsupported_claim(tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    pipeline = DocumentPipeline(tmp_path / "artifacts", tmp_path / "templates")
    context = pipeline.build_resume_context(job, facts)
    validation = pipeline.validate_plain_text(["I led a 200 person org."], context)
    assert validation["valid"] is False
    assert validation["failure_reason"] == "plain_text_contains_unsupported_lines"


def test_validate_plain_text_allows_grounded_resume_draft_lines(tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    pipeline = DocumentPipeline(tmp_path / "artifacts", tmp_path / "templates")
    artifact_draft = ArtifactDraft(
        resume_draft=ResumeDraft(
            headline="Platform Engineer | Python, FastAPI, SQL",
            summary_lines=[
                "Built reliable backend systems.",
                "Shipped a compliant job application tool.",
            ],
            custom_bullets=["Built reliable backend systems."],
        ),
        cover_letter_draft=CoverLetterDraft(paragraphs=["I am applying for the Software Engineer role at Acme."]),
    )
    context = pipeline.build_resume_context(job, facts, artifact_draft=artifact_draft)
    validation = pipeline.validate_plain_text(
        [
            "Test User",
            "user@example.com",
            "555-0100",
            "Platform Engineer | Python, FastAPI, SQL",
            "Software Engineer",
            "Built reliable backend systems.",
            "Shipped a compliant job application tool.",
            "Skills: Python",
        ],
        context,
    )

    assert validation["valid"] is True
    assert validation["unsupported_lines"] == []


def test_resume_text_cleanup_drops_trailing_single_character_lines() -> None:
    cleaned = drop_trailing_single_character_lines("Test User\nBuilt reliable backend systems.\n-\nD\ne\nm\n")
    assert cleaned == "Test User\nBuilt reliable backend systems."


def test_resume_text_cleanup_drops_single_character_runs_before_supported_text() -> None:
    cleaned = drop_trailing_single_character_lines("Test User\nBuilt reliable backend systems.\n-\nD\ne\nm\nSkills: Python\n")
    assert cleaned == "Test User\nBuilt reliable backend systems.\nSkills: Python"


def test_build_application_artifacts_blocks_invalid_pdf(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    pipeline = DocumentPipeline(tmp_path / "artifacts", tmp_path / "templates")

    def fake_render(self, template_name: str, base_name: str, context: dict):
        path = self.artifacts_dir / f"{base_name}.{template_name.replace('.typ', '')}.pdf"
        return RenderedArtifact(kind="pdf", path=path, content_hash="", validation_results={"valid": False, "failure_reason": "typst_compile_failed"})

    monkeypatch.setattr(DocumentPipeline, "render_typst", fake_render)
    with pytest.raises(ValueError, match="typst_compile_failed"):
        pipeline.build_application_artifacts(job, facts)


def test_build_resume_context_requires_contact(tmp_path: Path, job: NormalizedJobPosting) -> None:
    pipeline = DocumentPipeline(tmp_path / "artifacts", tmp_path / "templates")
    with pytest.raises(ValueError, match="Missing required contact facts"):
        pipeline.build_resume_context(job, [])


def test_build_cover_letter_payload_uses_local_template(tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    template_path = tmp_path / 'cover_letter_template.json'
    template_path.write_text(
        json.dumps(
            {
                'salutation': 'Dear Hiring Team at {company_name},',
                'paragraphs': ['I am applying for the {job_title} role at {company_name}.'],
                'closing': 'Sincerely,',
                'signature_name': '{name}',
            }
        ),
        encoding='utf-8',
    )
    pipeline = DocumentPipeline(
        tmp_path / 'artifacts',
        tmp_path / 'templates',
        DocumentTemplateConfig(cover_letter_template_path=template_path),
    )
    context = pipeline.build_resume_context(job, facts)

    payload = pipeline.build_cover_letter_payload(context)

    assert payload['source'] == 'local_template'
    assert payload['salutation'] == 'Dear Hiring Team at Acme,'
    assert payload['paragraphs'][0] == 'I am applying for the Software Engineer role at Acme.'
    assert payload['signature_name'] == 'Test User'



def test_build_latex_cover_letter_source_uses_pdflatex_safe_preamble(tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    pipeline = DocumentPipeline(tmp_path / 'artifacts', tmp_path / 'templates')
    context = pipeline.build_resume_context(job, facts)
    context['cover_letter'] = pipeline.build_cover_letter_payload(context)

    source = pipeline._build_latex_cover_letter_source(context)

    assert '\\usepackage{lmodern}' in source
    assert '\\microtypesetup{expansion=false}' in source

def test_build_application_artifacts_uses_latex_resume_renderer(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    template_path = tmp_path / 'cover_letter_template.json'
    template_path.write_text(
        json.dumps(
            {
                'salutation': 'Dear Hiring Team at {company_name},',
                'paragraphs': ['I am applying for the {job_title} role at {company_name}.'],
                'closing': 'Sincerely,',
                'signature_name': '{name}',
            }
        ),
        encoding='utf-8',
    )
    tex_path = tmp_path / 'resume.tex'
    tex_path.write_text(r'\\documentclass{article}\\begin{document}resume\\end{document}', encoding='utf-8')
    pipeline = DocumentPipeline(
        tmp_path / 'artifacts',
        tmp_path / 'templates',
        DocumentTemplateConfig(
            resume_renderer='latex',
            resume_template_path=tex_path,
            cover_letter_template_path=template_path,
        ),
    )
    calls: list[str] = []

    def fake_render_latex(self, base_name: str, context: dict):
        calls.append('latex')
        path = self.artifacts_dir / f'{base_name}.resume.pdf'
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(kind='pdf', path=path, content_hash='pdf-hash', validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 32})

    def fake_resume_text_from_pdf(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict):
        calls.append('resume_text_from_pdf')
        path = self.artifacts_dir / f'{base_name}.resume.txt'
        path.write_text('Test User\nuser@example.com', encoding='utf-8')
        return RenderedArtifact(kind='resume', path=path, content_hash='text-hash', validation_results={'valid': True, 'text_length': 28, 'contains_placeholder': False, 'missing_contact_fields': []})

    def fake_render_typst(self, template_name: str, base_name: str, context: dict):
        calls.append(template_name)
        path = self.artifacts_dir / f"{base_name}.{template_name.replace('.typ', '')}.pdf"
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(kind='pdf', path=path, content_hash='typst-hash', validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 64})

    def fake_render_latex_cover_letter(self, base_name: str, context: dict):
        calls.append('latex_cover_letter')
        path = self.artifacts_dir / f'{base_name}.cover_letter.pdf'
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(kind='pdf', path=path, content_hash='cl-hash', validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 64})

    monkeypatch.setattr(DocumentPipeline, 'render_latex_resume', fake_render_latex)
    monkeypatch.setattr(DocumentPipeline, 'write_resume_text_from_pdf', fake_resume_text_from_pdf)
    monkeypatch.setattr(DocumentPipeline, 'render_latex_cover_letter', fake_render_latex_cover_letter)

    artifacts = pipeline.build_application_artifacts(job, facts)

    assert len(artifacts) == 5
    assert 'latex' in calls
    assert 'latex_cover_letter' in calls
    assert 'resume.typ' not in calls
    assert 'cover_letter.typ' not in calls
    assert any(artifact.path.name.endswith('resume.pdf') for artifact in artifacts)
    assert any(artifact.path.name.endswith('cover_letter.pdf') for artifact in artifacts)

def test_render_latex_resume_prefers_xelatex(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    tex_path = tmp_path / 'resume.tex'
    tex_path.write_text('\\\\documentclass{article}\\\\begin{document}resume\\\\end{document}', encoding='utf-8')
    pipeline = DocumentPipeline(
        tmp_path / 'artifacts',
        tmp_path / 'templates',
        DocumentTemplateConfig(resume_renderer='latex', resume_template_path=tex_path),
    )
    context = pipeline.build_resume_context(job, facts)
    commands: list[list[str]] = []

    monkeypatch.setattr('findmyjob.documents.pipeline.find_latex_engine', lambda: 'xelatex')

    latex_env: dict[str, str] = {}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool, env=None):
        commands.append(command)
        latex_env.update(env or {})
        build_dir = Path(command[5])
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / 'resume.pdf').write_bytes(b'%PDF-1.4\n%stub\n')

        class Completed:
            returncode = 0
            stdout = ''
            stderr = ''

        return Completed()

    monkeypatch.setattr('findmyjob.documents.pipeline.subprocess.run', fake_run)
    monkeypatch.setattr(DocumentPipeline, 'validate_pdf', lambda self, pdf_path, expect_one_page, context: {'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 12})

    artifact = pipeline.render_latex_resume('example', context)

    assert commands
    assert commands[0][0] == 'xelatex'
    assert Path(latex_env['MIKTEX_USERCONFIG']).name == 'config'
    assert Path(latex_env['MIKTEX_USERDATA']).name == 'data'
    assert (tmp_path / 'artifacts' / '.miktex' / 'config').exists()
    assert (tmp_path / 'artifacts' / '.miktex' / 'data').exists()
    assert artifact.validation_results['valid'] is True



def test_render_typst_uses_template_relative_context_path(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    template_dir = tmp_path / 'templates'
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / 'cover_letter.typ').write_text('test template', encoding='utf-8')
    pipeline = DocumentPipeline(tmp_path / 'artifacts', template_dir)
    context = pipeline.build_resume_context(job, facts)
    commands: list[list[str]] = []

    monkeypatch.setattr('findmyjob.documents.pipeline.find_typst_executable', lambda: 'typst')

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool, env=None):
        _ = env
        commands.append(command)
        Path(command[5]).write_bytes(b'%PDF-1.4\n%stub\n')

        class Completed:
            returncode = 0
            stdout = ''
            stderr = ''

        return Completed()

    monkeypatch.setattr('findmyjob.documents.pipeline.subprocess.run', fake_run)
    monkeypatch.setattr(DocumentPipeline, 'validate_pdf', lambda self, pdf_path, expect_one_page, context: {'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 12})

    artifact = pipeline.render_typst('cover_letter.typ', 'example', context)

    assert commands
    assert commands[0][2] == '--root'
    assert commands[0][3] == str(tmp_path)
    assert commands[0][6] == '--input=context=../artifacts/example.render.json'
    assert artifact.validation_results['valid'] is True


def test_validate_generated_latex_rejects_preamble_changes(tmp_path: Path) -> None:
    pipeline = DocumentPipeline(tmp_path / 'artifacts', tmp_path / 'templates')
    template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nresume\n\\end{document}\n"
    generated = "\\documentclass{article}\n\\usepackage{geometry}\n\\begin{document}\nresume\n\\end{document}\n"

    with pytest.raises(ValueError, match='preamble'):
        pipeline._validate_generated_latex(generated, template)


def test_validate_generated_latex_rejects_non_package_preamble_edits(tmp_path: Path) -> None:
    pipeline = DocumentPipeline(tmp_path / 'artifacts', tmp_path / 'templates')
    template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\titleformat{\\section}{\\bfseries}{}{0em}{}\n\\begin{document}\nresume\n\\end{document}\n"
    generated = "\\documentclass{article}\n\\usepackage{lmodern}\n\\titlespace{\\section}{\\bfseries}{}{0em}{}\n\\begin{document}\nresume\n\\end{document}\n"

    with pytest.raises(ValueError, match='preamble'):
        pipeline._validate_generated_latex(generated, template)


def test_build_application_artifacts_latex_direct_generates_sources(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    artifacts_dir = tmp_path / 'artifacts'
    pipeline = DocumentPipeline(
        artifacts_dir,
        tmp_path / 'templates',
        DocumentTemplateConfig(resume_renderer='latex_direct'),
    )
    resume_template_path = tmp_path / 'resume.tex'
    cover_template_path = tmp_path / 'cover.tex'
    resume_template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nResume body\n\\end{document}\n"
    cover_template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nCover body\n\\end{document}\n"
    resume_template_path.write_text(resume_template, encoding='utf-8')
    cover_template_path.write_text(cover_template, encoding='utf-8')

    class _Router:
        def __init__(self) -> None:
            self.responses = [
                "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nOptimized resume body\n\\end{document}\n",
                "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nOptimized cover body\n\\end{document}\n",
            ]

        async def generate_text(self, role, prompt, system_prompt=None):
            _ = role
            _ = prompt
            _ = system_prompt
            return self.responses.pop(0)

    def fake_compile(self, latex_source: str, base_name: str, artifact_kind: str) -> RenderedArtifact:
        _ = latex_source
        path = self.artifacts_dir / f'{base_name}.{artifact_kind}.pdf'
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(kind='pdf', path=path, content_hash=f'{artifact_kind}-hash', validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 16})

    def fake_resume_text(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict) -> RenderedArtifact:
        _ = pdf_artifact
        _ = context
        path = self.artifacts_dir / f'{base_name}.resume.txt'
        path.write_text('Test User\nuser@example.com\n', encoding='utf-8')
        return RenderedArtifact(kind='resume', path=path, content_hash='resume-text-hash', validation_results={'valid': True, 'text_length': 27, 'contains_placeholder': False, 'missing_contact_fields': []})

    def fake_cover_text(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict) -> RenderedArtifact:
        _ = pdf_artifact
        _ = context
        path = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        path.write_text('Dear Acme Hiring Team,\n\nTest User\n', encoding='utf-8')
        return RenderedArtifact(kind='cover_letter', path=path, content_hash='cover-text-hash', validation_results={'valid': True, 'text_length': 34, 'contains_placeholder': False, 'missing_required_fields': []})

    monkeypatch.setattr(DocumentPipeline, 'compile_raw_latex', fake_compile)
    monkeypatch.setattr(DocumentPipeline, 'write_resume_text_from_pdf', fake_resume_text)
    monkeypatch.setattr(DocumentPipeline, 'write_cover_letter_text_from_pdf', fake_cover_text)

    router = _Router()
    artifacts = anyio.run(
        partial(
            pipeline.build_application_artifacts_latex_direct,
            job,
            facts,
            router,
            resume_template_path=resume_template_path,
            cover_letter_template_path=cover_template_path,
        )
    )

    base_name = pipeline.deterministic_base_name(job)
    assert len(artifacts) == 4
    assert (artifacts_dir / f'{base_name}.resume.tex').exists()
    assert (artifacts_dir / f'{base_name}.cover_letter.tex').exists()
    assert router.responses == []


def test_build_application_artifacts_latex_direct_retries_on_one_page_overflow(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    artifacts_dir = tmp_path / 'artifacts'
    pipeline = DocumentPipeline(
        artifacts_dir,
        tmp_path / 'templates',
        DocumentTemplateConfig(resume_renderer='latex_direct'),
    )
    resume_template_path = tmp_path / 'resume.tex'
    cover_template_path = tmp_path / 'cover.tex'
    resume_template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nResume body\n\\end{document}\n"
    cover_template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nCover body\n\\end{document}\n"
    resume_template_path.write_text(resume_template, encoding='utf-8')
    cover_template_path.write_text(cover_template, encoding='utf-8')

    class _Router:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.responses = [
                "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nResume v1\n\\end{document}\n",
                "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nResume v2\n\\end{document}\n",
                "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nCover v1\n\\end{document}\n",
            ]

        async def generate_text(self, role, prompt, system_prompt=None):
            _ = role
            _ = system_prompt
            self.prompts.append(prompt)
            return self.responses.pop(0)

    compile_counts = {'resume': 0, 'cover_letter': 0}

    def fake_compile(self, latex_source: str, base_name: str, artifact_kind: str) -> RenderedArtifact:
        _ = latex_source
        path = self.artifacts_dir / f'{base_name}.{artifact_kind}.pdf'
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        compile_counts[artifact_kind] += 1
        if artifact_kind == 'resume' and compile_counts['resume'] == 1:
            return RenderedArtifact(
                kind='pdf',
                path=path,
                content_hash='resume-overflow',
                validation_results={
                    'valid': False,
                    'failure_reason': 'resume_exceeds_one_page',
                    'page_count': 2,
                    'overflow_line_count': 3,
                },
            )
        return RenderedArtifact(
            kind='pdf',
            path=path,
            content_hash=f'{artifact_kind}-ok',
            validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 16},
        )

    def fake_resume_text(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict) -> RenderedArtifact:
        _ = pdf_artifact
        _ = context
        path = self.artifacts_dir / f'{base_name}.resume.txt'
        path.write_text('Test User\nuser@example.com\n', encoding='utf-8')
        return RenderedArtifact(kind='resume', path=path, content_hash='resume-text-hash', validation_results={'valid': True, 'text_length': 27, 'contains_placeholder': False, 'missing_contact_fields': []})

    def fake_cover_text(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict) -> RenderedArtifact:
        _ = pdf_artifact
        _ = context
        path = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        path.write_text('Dear Acme Hiring Team,\n\nTest User\n', encoding='utf-8')
        return RenderedArtifact(kind='cover_letter', path=path, content_hash='cover-text-hash', validation_results={'valid': True, 'text_length': 34, 'contains_placeholder': False, 'missing_required_fields': []})

    monkeypatch.setattr(DocumentPipeline, 'compile_raw_latex', fake_compile)
    monkeypatch.setattr(DocumentPipeline, 'write_resume_text_from_pdf', fake_resume_text)
    monkeypatch.setattr(DocumentPipeline, 'write_cover_letter_text_from_pdf', fake_cover_text)

    router = _Router()
    artifacts = anyio.run(
        partial(
            pipeline.build_application_artifacts_latex_direct,
            job,
            facts,
            router,
            resume_template_path=resume_template_path,
            cover_letter_template_path=cover_template_path,
        )
    )

    assert len(artifacts) == 4
    assert compile_counts['resume'] == 2
    assert router.prompts[0].startswith('/no-think')
    assert 'Do not default to minimal edits.' in router.prompts[0]
    assert 'GROUNDED CANDIDATE FACTS:' in router.prompts[0]
    assert 'TARGET JOB:' in router.prompts[0]
    assert 'reduce the content by roughly 5 lines total' in router.prompts[1]


def test_build_application_artifacts_latex_direct_retries_on_preamble_validation_error(monkeypatch, tmp_path: Path, job: NormalizedJobPosting, facts: list[ProfileFact]) -> None:
    artifacts_dir = tmp_path / 'artifacts'
    pipeline = DocumentPipeline(
        artifacts_dir,
        tmp_path / 'templates',
        DocumentTemplateConfig(resume_renderer='latex_direct'),
    )
    resume_template_path = tmp_path / 'resume.tex'
    cover_template_path = tmp_path / 'cover.tex'
    resume_template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\titleformat{\\section}{\\bfseries}{}{0em}{}\n\\begin{document}\nResume body\n\\end{document}\n"
    cover_template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nCover body\n\\end{document}\n"
    resume_template_path.write_text(resume_template, encoding='utf-8')
    cover_template_path.write_text(cover_template, encoding='utf-8')

    class _Router:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.responses = [
                "\\documentclass{article}\n\\usepackage{lmodern}\n\\titlespace{\\section}{\\bfseries}{}{0em}{}\n\\begin{document}\nResume v1\n\\end{document}\n",
                resume_template.replace("Resume body", "Resume v2"),
                cover_template.replace("Cover body", "Cover v1"),
            ]

        async def generate_text(self, role, prompt, system_prompt=None):
            _ = role
            _ = system_prompt
            self.prompts.append(prompt)
            return self.responses.pop(0)

    def fake_compile(self, latex_source: str, base_name: str, artifact_kind: str) -> RenderedArtifact:
        _ = latex_source
        path = self.artifacts_dir / f'{base_name}.{artifact_kind}.pdf'
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(
            kind='pdf',
            path=path,
            content_hash=f'{artifact_kind}-ok',
            validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 16},
        )

    def fake_resume_text(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict) -> RenderedArtifact:
        _ = pdf_artifact
        _ = context
        path = self.artifacts_dir / f'{base_name}.resume.txt'
        path.write_text('Test User\nuser@example.com\n', encoding='utf-8')
        return RenderedArtifact(kind='resume', path=path, content_hash='resume-text-hash', validation_results={'valid': True, 'text_length': 27, 'contains_placeholder': False, 'missing_contact_fields': []})

    def fake_cover_text(self, base_name: str, pdf_artifact: RenderedArtifact, context: dict) -> RenderedArtifact:
        _ = pdf_artifact
        _ = context
        path = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        path.write_text('Dear Acme Hiring Team,\n\nTest User\n', encoding='utf-8')
        return RenderedArtifact(kind='cover_letter', path=path, content_hash='cover-text-hash', validation_results={'valid': True, 'text_length': 34, 'contains_placeholder': False, 'missing_required_fields': []})

    monkeypatch.setattr(DocumentPipeline, 'compile_raw_latex', fake_compile)
    monkeypatch.setattr(DocumentPipeline, 'write_resume_text_from_pdf', fake_resume_text)
    monkeypatch.setattr(DocumentPipeline, 'write_cover_letter_text_from_pdf', fake_cover_text)

    router = _Router()
    artifacts = anyio.run(
        partial(
            pipeline.build_application_artifacts_latex_direct,
            job,
            facts,
            router,
            resume_template_path=resume_template_path,
            cover_letter_template_path=cover_template_path,
        )
    )

    assert len(artifacts) == 4
    assert len(router.prompts) == 3
    assert 'changed protected template structure' in router.prompts[1]


def test_generate_latex_cover_letter_prompt_retargets_recipient_and_subject(job: NormalizedJobPosting, facts: list[ProfileFact], tmp_path: Path) -> None:
    pipeline = DocumentPipeline(
        tmp_path / 'artifacts',
        tmp_path / 'templates',
        DocumentTemplateConfig(resume_renderer='latex_direct'),
    )
    context = pipeline.build_resume_context(job, facts)
    pipeline._raw_latex_validation_context = context

    class _Router:
        async def generate_text(self, role, prompt, system_prompt=None):
            _ = role
            _ = system_prompt
            self.prompt = prompt
            return "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nCover body\n\\end{document}\n"

    router = _Router()
    template = "\\documentclass{article}\n\\usepackage{lmodern}\n\\begin{document}\nCover body\n\\end{document}\n"

    anyio.run(
        pipeline.generate_latex_cover_letter,
        template,
        job.description,
        job.company_name,
        job.title,
        router,
        'cover_letter_writer',
    )

    assert 'Update the recipient block and subject so they clearly reference the target company and role.' in router.prompt
    assert 'Replace outdated academic or unrelated recipient/subject text if present in the template.' in router.prompt
    assert 'Write exactly 3 substantive body paragraphs.' in router.prompt
    assert 'GROUNDED CANDIDATE FACTS:' in router.prompt




