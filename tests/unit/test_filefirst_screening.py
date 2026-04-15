from __future__ import annotations

from pathlib import Path

from findmyjob.filefirst.models import FileFact, InboxJob
from findmyjob.filefirst.screening import _screen_context, normalize_screening_payload, override_screening, reset_screening, screen_job
from findmyjob.filefirst.workspace import FileWorkspace


def _seed_workspace(tmp_path: Path) -> FileWorkspace:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n')
    ws.save_facts(
        [
            FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
            FileFact(fact_id='work.primary', kind='work', payload={'summary': 'Built local-first automation tooling.'}),
        ]
    )
    job = InboxJob(
        job_id='job-screen-1',
        company='Acme',
        company_key='acme',
        title='Software Engineering Intern',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='100',
        url='https://boards.greenhouse.io/acme/jobs/100',
        apply_url='https://boards.greenhouse.io/acme/jobs/100',
        location='Remote',
        description='Internship role for summer 2026.',
        workflow_state='pending',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-screen-1',
        duplicate_cluster_key='acme-intern',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    return ws


def test_normalize_screening_payload_handles_reason_aliases() -> None:
    decision = normalize_screening_payload(
        {
            'approved': 'no',
            'reason': 'This is an internship role.',
            'confidence': '0.91',
            'is_internship': True,
            'years_experience': '0-1 years',
            'notes': ['Entry point mismatch'],
        }
    )

    assert decision.approved is False
    assert decision.reasons == ['This is an internship role.']
    assert decision.internship_like is True
    assert decision.years_experience_signal == '0-1 years'
    assert decision.notes == 'Entry point mismatch'


def test_low_confidence_rejection_is_held_for_review() -> None:
    decision = normalize_screening_payload(
        {
            'approved': False,
            'reasons': ['Ambiguous seniority signal.'],
            'confidence': 0.2,
            'notes': 'Model was uncertain.',
        }
    )

    assert decision.approved is False
    assert 'low_confidence_held_for_review' in decision.reasons
    assert 'low_confidence_rejection<0.4; held_for_review' in (decision.notes or '')


def test_screen_context_is_dict_and_strips_html(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    job = ws.load_job('job-screen-1')
    assert job is not None
    job = job.model_copy(
        update={
            'title': 'Backend Software Engineer',
            'description': '<div>Build Python &amp; data systems for customers.</div>',
        }
    )
    context = _screen_context(ws, job)

    assert isinstance(context, dict)
    assert not isinstance(context, tuple)
    assert context['job']['description'] == 'Build Python & data systems for customers.'


def test_screen_job_persists_rejected_state(monkeypatch, tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)

    monkeypatch.setattr(
        'findmyjob.filefirst.screening.ModeRunner.run_json',
        lambda self, mode_name, context: {
            'approved': False,
            'reasons': ['Internship roles are out of scope for launch.'],
            'confidence': 0.88,
            'internship_like': True,
            'seniority_too_high': False,
            'years_experience_signal': '0-1 years',
            'notes': 'Skip internship postings.',
        },
    )

    job, screening = screen_job(ws, 'job-screen-1')

    assert screening.approved is False
    assert job.workflow_state == 'screened_out'
    reloaded = ws.load_job('job-screen-1')
    assert reloaded is not None
    assert reloaded.screening is not None
    assert reloaded.screening.internship_like is True
    assert ws.load_inbox()[0].workflow_state == 'screened_out'


def test_override_screening_promotes_job(monkeypatch, tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    monkeypatch.setattr(
        'findmyjob.filefirst.screening.ModeRunner.run_json',
        lambda self, mode_name, context: {
            'approved': False,
            'reasons': ['Too senior for the launch filter.'],
            'confidence': 0.75,
            'internship_like': False,
            'seniority_too_high': True,
            'years_experience_signal': '5+ years',
            'notes': 'Rejected by default.',
        },
    )
    screen_job(ws, 'job-screen-1')

    job, screening = override_screening(ws, 'job-screen-1', approved=True, note='Operator override for rehearsal')

    assert screening.approved is True
    assert screening.overridden is True
    assert job.workflow_state == 'pending'


def test_reset_screening_clears_decision(monkeypatch, tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    monkeypatch.setattr(
        'findmyjob.filefirst.screening.ModeRunner.run_json',
        lambda self, mode_name, context: {
            'approved': False,
            'reasons': ['Rejected during screening.'],
            'confidence': 0.88,
            'internship_like': True,
            'seniority_too_high': False,
            'years_experience_signal': '0-1 years',
            'notes': 'Skip internship postings.',
        },
    )
    screen_job(ws, 'job-screen-1')

    job = reset_screening(ws, 'job-screen-1')

    assert job.workflow_state == 'pending'
    assert job.screening is None



def test_screen_job_uses_hard_reject_without_model(monkeypatch, tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    original = ws.load_job('job-screen-1')
    assert original is not None
    blocked = original.model_copy(update={
        'ats_family': 'workday',
        'hard_reject_reason': 'unsupported_ats:workday',
        'auth_reject_reason': 'no_sponsorship',
        'rehearsal_eligible': False,
    })
    ws.save_job(blocked)
    ws.upsert_inbox_jobs([blocked])

    def fail_run_json(*args, **kwargs):
        raise AssertionError('Gemma screening should not run for deterministic hard rejects')

    monkeypatch.setattr('findmyjob.filefirst.screening.ModeRunner.run_json', fail_run_json)

    job, screening = screen_job(ws, 'job-screen-1')

    assert screening.approved is False
    assert any('Authorization filter:' in reason for reason in screening.reasons)
    assert any('Hard filter:' in reason for reason in screening.reasons)
    assert job.workflow_state == 'screened_out'
