from __future__ import annotations

from datetime import datetime, timezone

from findmyjob.core.enums import FactKind, Sensitivity, WorkplaceType
from findmyjob.core.types import NormalizedJobPosting, ProfileFact
from findmyjob.documents.pipeline import RenderedArtifact
from findmyjob.filefirst.models import ApplicationEntry, EvaluationResult, FileFact, InboxJob
from findmyjob.filefirst.render import _compact_job_for_llm, _cover_letter_paragraphs, build_pdf_for_target
from findmyjob.filefirst.workspace import FileWorkspace


def _job(*, description: str = 'Build backend systems.') -> InboxJob:
    return InboxJob(
        job_id='job-render-1',
        company='OpenAI',
        company_key='openai',
        title='Backend Engineer',
        source='ashby',
        source_kind='ashby',
        source_job_id='render-1',
        url='https://jobs.ashbyhq.com/openai/render-1',
        apply_url='https://jobs.ashbyhq.com/openai/render-1',
        location='Remote',
        description=description,
        workflow_state='evaluated',
        board_family='ashby',
        automation_tier='auto_submit_supported',
        job_identity_key='job-render-1',
        duplicate_cluster_key='openai-backend-engineer',
    )


def _evaluation() -> EvaluationResult:
    return EvaluationResult(
        job_id='job-render-1',
        company='OpenAI',
        role='Backend Engineer',
        source='ashby',
        url='https://jobs.ashbyhq.com/openai/render-1',
    )


def test_compact_job_for_llm_strips_html_entities() -> None:
    compact = _compact_job_for_llm(_job(description='<div>Build ML &amp; Python systems.</div>'))
    assert compact.description == 'Build ML & Python systems.'


def test_cover_letter_paragraphs_detect_duplicate_intro_case_insensitively() -> None:
    paragraphs = ['I am applying for the backend engineer role at openai because the mission is compelling.']

    result = _cover_letter_paragraphs(_job(), _evaluation(), paragraphs)

    assert len(result) == 1
    assert result[0] == paragraphs[0]


def test_cover_letter_paragraphs_strip_html_and_placeholder_text() -> None:
    paragraphs = [
        '<div>I build reliable distributed systems.</div>',
        'I understand there is still room to grow, especially around N.',
        'TODO replace this paragraph.',
    ]

    result = _cover_letter_paragraphs(_job(), _evaluation(), paragraphs)

    assert any('I build reliable distributed systems.' in paragraph for paragraph in result)
    assert all('<div' not in paragraph for paragraph in result)
    assert all('especially around N' not in paragraph for paragraph in result)
    assert all('TODO' not in paragraph for paragraph in result)


def test_build_pdf_for_target_latex_direct_skips_json_drafting_writer(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n')
    ws.save_facts([
        FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
        FileFact(fact_id='work.primary', kind='work', payload={'summary': 'Built reliable systems.'}),
    ])
    job = InboxJob(
        job_id='job-render-latex-direct',
        company='Acme',
        company_key='acme',
        title='Software Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='123',
        url='https://boards.greenhouse.io/acme/jobs/123',
        apply_url='https://boards.greenhouse.io/acme/jobs/123',
        location='Remote',
        description='Build reliable backend systems.',
        workflow_state='evaluated',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-render-latex-direct',
        duplicate_cluster_key='acme-software-engineer',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    ws.save_evaluation(
        EvaluationResult(
            job_id='job-render-latex-direct',
            company='Acme',
            role='Software Engineer',
            source='greenhouse',
            url=job.url,
            score=4.2,
            grade='A',
        )
    )
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id='job-render-latex-direct',
            date='2026-04-11',
            company='Acme',
            role='Software Engineer',
            score=4.2,
            grade='A',
            status='Evaluated',
            pdf=False,
            report='reports/001-acme.md',
            url=job.url,
            source='greenhouse',
        )
    )

    def fail_if_called(*args, **kwargs):
        _ = (args, kwargs)
        raise AssertionError('build_resume_plan_with_router should not run for latex_direct rendering')

    async def fake_build_application_artifacts_latex_direct(self, job, facts, router, *, resume_template_path, cover_letter_template_path):
        _ = (facts, router, resume_template_path, cover_letter_template_path)
        base_name = self.deterministic_base_name(job)
        resume_pdf = self.artifacts_dir / f'{base_name}.resume.pdf'
        cover_pdf = self.artifacts_dir / f'{base_name}.cover_letter.pdf'
        resume_txt = self.artifacts_dir / f'{base_name}.resume.txt'
        cover_txt = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        for path in (resume_pdf, cover_pdf):
            path.write_bytes(b'%PDF-1.4\n%stub\n')
        resume_txt.write_text('Test User\nuser@example.com\n', encoding='utf-8')
        cover_txt.write_text('Dear Acme Hiring Team,\n\nSincerely,\n', encoding='utf-8')
        return [
            RenderedArtifact(kind='pdf', path=resume_pdf, content_hash='resume', validation_results={'valid': True, 'page_count': 1}),
            RenderedArtifact(kind='pdf', path=cover_pdf, content_hash='cover', validation_results={'valid': True, 'page_count': 1}),
            RenderedArtifact(kind='resume', path=resume_txt, content_hash='resume-text', validation_results={'valid': True}),
            RenderedArtifact(kind='cover_letter', path=cover_txt, content_hash='cover-text', validation_results={'valid': True}),
        ]

    monkeypatch.setattr('findmyjob.filefirst.render.build_resume_plan_with_router', fail_if_called)
    monkeypatch.setattr(
        'findmyjob.filefirst.render._template_bridge_details',
        lambda workspace: {
            'configured': True,
            'requested': True,
            'resume_renderer': 'latex_direct',
            'resume_template_path': workspace.root / 'my_personal_information' / 'CV_editable.tex',
            'cover_letter_template_path': workspace.root / 'my_personal_information' / 'CoverLetter_editable.tex',
            'missing_resume_template': False,
        },
    )
    monkeypatch.setattr('findmyjob.filefirst.advanced_models.load_model_router', lambda workspace: object())
    monkeypatch.setattr(
        'findmyjob.documents.pipeline.DocumentPipeline.build_application_artifacts_latex_direct',
        fake_build_application_artifacts_latex_direct,
    )

    result = build_pdf_for_target(ws, 'job-render-latex-direct')

    assert result['success'] is True
    assert result['renderer'] == 'latex_direct'


def test_build_pdf_for_target_chatgpt_download_uses_chatgpt_service(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n')
    ws.save_facts([
        FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
    ])
    job = _job(description='Build backend systems.')
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    ws.save_evaluation(_evaluation())
    ws.upsert_application(
        ApplicationEntry(
            id='001',
            job_id='job-render-1',
            date='2026-04-12',
            company='OpenAI',
            role='Backend Engineer',
            score=4.5,
            grade='A',
            status='Evaluated',
            pdf=False,
            report='reports/001-openai.md',
            url=job.url,
            source='ashby',
        )
    )

    monkeypatch.setattr(
        'findmyjob.filefirst.render._template_bridge_details',
        lambda workspace: {
            'configured': True,
            'requested': True,
            'resume_renderer': 'chatgpt_download',
            'resume_template_path': None,
            'cover_letter_template_path': None,
            'missing_resume_template': False,
            'chatgpt_drafting': {
                'completion_start_marker': '[[PDF_OUTPUT_READY]]',
                'completion_end_marker': '[[PDF_OUTPUT_COMPLETE]]',
            },
        },
    )
    monkeypatch.setattr(
        'findmyjob.filefirst.render.ChatGPTDraftingService.draft',
        lambda self, *, job, evaluation, application_id, on_date=None: {
            'success': True,
            'job_id': job.job_id,
            'application_id': application_id,
            'renderer': 'chatgpt_download',
            'template_bridge_used': False,
            'resume_template_path': None,
            'cover_letter_template_path': None,
            'html_path': None,
            'pdf_path': 'output/cv-001-openai-2026-04-12.pdf',
            'cover_letter_path': 'output/cover-letter-001-openai.pdf',
            'resume_text_path': 'output/cv-001-openai-2026-04-12.resume.txt',
            'cover_letter_text_path': 'output/cv-001-openai-2026-04-12.cover_letter.txt',
            'warnings': [],
            'render_error': None,
            'draft': {'provider': 'chatgpt_custom_gpt'},
        },
    )

    result = build_pdf_for_target(ws, 'job-render-1')

    assert result['success'] is True
    assert result['renderer'] == 'chatgpt_download'
