from __future__ import annotations

from pathlib import Path

from findmyjob.documents.pipeline import DocumentPipeline
from findmyjob.filefirst.evaluate import _resolve_target, evaluate_target, run_pipeline
from findmyjob.filefirst.models import ApplicationEntry, EvaluationResult, FileFact, InboxJob, ResumePlan
from findmyjob.filefirst.workspace import FileWorkspace



def _seed_workspace(tmp_path: Path) -> FileWorkspace:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n\n- Backend engineer\n')
    ws.save_facts(
        [
            FileFact(
                fact_id='contact.primary',
                kind='contact',
                payload={
                    'name': 'Test User',
                    'email': 'test@example.com',
                    'phone': '555-111-2222',
                    'linkedin': 'linkedin.com/in/test-user',
                },
            ),
            FileFact(
                fact_id='work.primary',
                kind='work',
                payload={
                    'title': 'Backend Engineer',
                    'company': 'Acme',
                    'summary': 'Built backend automation and APIs.',
                    'bullets': ['Improved job matching.', 'Built local tooling.'],
                },
            ),
            FileFact(
                fact_id='project.primary',
                kind='project',
                payload={'name': 'Career Ops', 'summary': 'Local file-first job workspace.'},
            ),
            FileFact(
                fact_id='skill.python',
                kind='skill',
                payload={'name': 'Python'},
            ),
        ]
    )
    job = InboxJob(
        job_id='job-100',
        company='Acme',
        company_key='acme',
        title='Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='100',
        url='https://boards.greenhouse.io/acme/jobs/100',
        apply_url='https://boards.greenhouse.io/acme/jobs/100',
        location='Remote - United States',
        description='Build backend services and internal tooling.',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-100',
        duplicate_cluster_key='acme-backend-engineer',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    return ws



def test_evaluate_target_writes_report_and_application(monkeypatch, tmp_path) -> None:
    ws = _seed_workspace(tmp_path)

    monkeypatch.setattr(
        'findmyjob.filefirst.evaluate.ModeRunner.run_json',
        lambda self, mode_name, context: {
            'company': 'Acme',
            'role': 'Backend Engineer',
            'archetype': 'Applied AI Engineer',
            'score': 4.7,
            'grade': 'A',
            'summary': 'Strong fit for local-first backend automation.',
            'keywords': ['python', 'backend', 'automation'],
            'fit_reasons': ['Strong Python background', 'Local tooling experience'],
            'gaps': ['No direct Claude integration in current stack'],
            'report_markdown': '# Evaluation\n\nStrong fit.',
            'resume_headline': 'Backend engineer for local AI systems',
            'resume_summary_lines': ['Built local agentic tooling.'],
            'selected_work_fact_ids': ['work.primary'],
            'selected_project_fact_ids': ['project.primary'],
            'selected_skill_fact_ids': ['skill.python'],
            'custom_bullets': ['Highlight local inference workflows.'],
            'cover_letter_paragraphs': ['Explain the local-first fit.'],
        },
    )

    result = evaluate_target(tmp_path, 'job-100')
    applications = ws.load_applications()
    evaluation = ws.load_evaluation('job-100')

    assert result['application_id'] == '001'
    assert (tmp_path / result['report_path']).exists()
    assert evaluation is not None
    assert evaluation.grade == 'A'
    assert len(applications) == 1
    assert applications[0].status == 'Evaluated'
    assert ws.load_inbox()[0].workflow_state == 'evaluated'


def test_evaluate_target_derives_grade_from_score(monkeypatch, tmp_path) -> None:
    ws = _seed_workspace(tmp_path)

    monkeypatch.setattr(
        'findmyjob.filefirst.evaluate.ModeRunner.run_json',
        lambda self, mode_name, context: {
            'company': 'Acme',
            'role': 'Backend Engineer',
            'score': 4.0,
            'grade': 'A',
            'summary': 'Strong fit for local-first backend automation.',
            'keywords': ['python'],
            'fit_reasons': ['Strong Python background'],
            'gaps': [],
            'report_markdown': '# Evaluation\n\nStrong fit.',
        },
    )

    result = evaluate_target(tmp_path, 'job-100')

    assert result['score'] == 4.0
    assert result['grade'] == 'B'
    assert ws.load_evaluation('job-100').grade == 'B'


def test_resolve_target_loads_job_once(monkeypatch, tmp_path) -> None:
    ws = _seed_workspace(tmp_path)
    original_load_job = FileWorkspace.load_job
    calls: list[str] = []

    def tracked_load_job(self, job_id: str):
        calls.append(job_id)
        return original_load_job(self, job_id)

    monkeypatch.setattr(FileWorkspace, 'load_job', tracked_load_job)

    job = _resolve_target(ws, 'job-100')

    assert job.job_id == 'job-100'
    assert calls == ['job-100']



def test_run_pipeline_generates_pdf_and_updates_tracker(monkeypatch, tmp_path) -> None:
    ws = _seed_workspace(tmp_path)

    def _fake_mode_json(self, mode_name, context):
        if mode_name == 'screen':
            return {
                'approved': True,
                'reasons': ['Entry-level backend role is in scope.'],
                'confidence': 0.92,
                'internship_like': False,
                'seniority_too_high': False,
                'years_experience_signal': '0-2 years',
                'notes': 'Safe to continue.',
            }
        if mode_name == 'pdf':
            return {
                'headline': 'Backend engineer for local inference',
                'summary_lines': ['Built local-first job automation tooling.'],
                'selected_work_fact_ids': ['work.primary'],
                'selected_project_fact_ids': ['project.primary'],
                'selected_skill_fact_ids': ['skill.python'],
                'custom_bullets': ['Mention local model serving and evaluation.'],
                'cover_letter_paragraphs': ['Local inference keeps cost and latency down.'],
            }
        return {
            'company': 'Acme',
            'role': 'Backend Engineer',
            'archetype': 'Applied AI Engineer',
            'score': 4.4,
            'grade': 'B',
            'summary': 'Good fit for backend and local inference work.',
            'keywords': ['python', 'fastapi', 'backend'],
            'fit_reasons': ['Backend systems experience'],
            'gaps': ['Needs more explicit multimodal examples'],
            'report_markdown': '# Evaluation\n\nGood fit.',
            'resume_headline': 'Backend engineer for local inference',
            'resume_summary_lines': ['Shipped local-first automation.'],
            'selected_work_fact_ids': ['work.primary'],
            'selected_project_fact_ids': ['project.primary'],
            'selected_skill_fact_ids': ['skill.python'],
            'custom_bullets': ['Emphasize local model operations.'],
            'cover_letter_paragraphs': ['Explain why local Gemma matters.'],
        }

    monkeypatch.setattr('findmyjob.filefirst.modes.ModeRunner.run_json', _fake_mode_json)
    monkeypatch.setattr(
        'findmyjob.filefirst.render.build_resume_plan_with_router',
        lambda ws, job, evaluation: (
            ResumePlan(
                headline='Backend engineer for local inference',
                summary_lines=['Built local-first job automation tooling.'],
                selected_work_fact_ids=['work.primary'],
                selected_project_fact_ids=['project.primary'],
                selected_skill_fact_ids=['skill.python'],
                custom_bullets=['Mention local model serving and evaluation.'],
                cover_letter_paragraphs=['Local inference keeps cost and latency down.'],
            ),
            {'writer_profile': 'test-writer', 'validation_profile': 'deterministic_validation', 'validation_issues': [], 'repair_attempted': False, 'repair_writer_profile': None, 'verified': True, 'adaptation_summary': '', 'signature_name': 'Test User', 'salutation': 'Dear Hiring Team,', 'closing': 'Sincerely,'},
        ),
    )

    def _fake_build_application_artifacts(self, job, facts, artifact_draft=None):
        _ = job
        _ = facts
        _ = artifact_draft
        base_name = 'acme-backend-engineer-greenhouse-100'
        resume_pdf = self.artifacts_dir / f'{base_name}.resume.pdf'
        cover_pdf = self.artifacts_dir / f'{base_name}.cover_letter.pdf'
        resume_txt = self.artifacts_dir / f'{base_name}.resume.txt'
        cover_txt = self.artifacts_dir / f'{base_name}.cover_letter.txt'
        for path in (resume_pdf, cover_pdf):
            path.write_bytes(b'%PDF-1.4\n%stub\n')
        resume_txt.write_text('Test User\nBuilt local-first job automation tooling.\n', encoding='utf-8')
        cover_txt.write_text('Dear Hiring Team,\n\nLocal inference keeps cost and latency down.\n', encoding='utf-8')
        return [
            type('Artifact', (), {'kind': 'pdf', 'path': resume_pdf, 'validation_results': {'valid': True}})(),
            type('Artifact', (), {'kind': 'pdf', 'path': cover_pdf, 'validation_results': {'valid': True}})(),
            type('Artifact', (), {'kind': 'resume', 'path': resume_txt, 'validation_results': {'valid': True}})(),
            type('Artifact', (), {'kind': 'cover_letter', 'path': cover_txt, 'validation_results': {'valid': True}})(),
        ]

    monkeypatch.setattr(DocumentPipeline, 'build_application_artifacts', _fake_build_application_artifacts)
    monkeypatch.setattr(DocumentPipeline, 'validation_warnings', lambda self, artifacts: [])

    result = run_pipeline(tmp_path)
    applications = ws.load_applications()

    assert result['processed'] == 1
    assert len(result['pdfs']) == 1
    assert result['pdfs'][0]['pdf_path'] is not None
    assert (tmp_path / result['pdfs'][0]['pdf_path']).exists()
    assert applications[0].pdf is True
    assert applications[0].status == 'PDF Ready'
    assert ws.load_inbox()[0].workflow_state == 'pdf_ready'
