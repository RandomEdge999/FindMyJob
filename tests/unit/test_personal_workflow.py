from __future__ import annotations

import pytest

from findmyjob.core.enums import ApplicationMode, ExperienceLevel, FactKind, JobLifecycleStatus, PersonalSuppressionScope, PersonalTriageStatus, ReviewStatus, RunStatus, Sensitivity, SponsorshipFit, SponsorshipSignal, WorkplaceType
from findmyjob.core.types import JobSearchQuery, NormalizedJobPosting, ProfileFact, QualificationResult, SavedSearch
from findmyjob.db.models import utcnow
from findmyjob.db.repositories import ApplicationRepository, AuditRepository, JobRepository, ProfileRepository, RunRepository, SavedSearchRepository, hash_content
from findmyjob.documents.pipeline import RenderedArtifact
from findmyjob.core.runtime import AppRuntime
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.personal.preferences import compose_personal_query, update_personal_preferences
from findmyjob.personal.workflow import build_personal_inbox, dismiss_job, explain_personal_job, latest_personal_daily_summary, preview_personal_cover_letter, preview_personal_resume, resolve_personal_queries, run_personal_daily, shortlist_job, unsuppress_job


@pytest.fixture()
def runtime(tmp_path: Path) -> AppRuntime:
    return AppRuntime.bootstrap(tmp_path)


def _seed_saved_searches(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = SavedSearchRepository(session)
        repo.save(SavedSearch(name='backend-core', query_payload=JobSearchQuery(title_keywords=['backend'], limit=50)))
        repo.save(SavedSearch(name='frontend-ui', query_payload=JobSearchQuery(title_keywords=['frontend'], limit=50)))


def _seed_profile(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        repo.upsert_fact(ProfileFact(fact_id='contact.primary', kind=FactKind.CONTACT, payload={'name': 'Test User', 'email': 'user@example.com'}, sensitivity=Sensitivity.LOW))
        repo.upsert_fact(ProfileFact(fact_id='work.primary', kind=FactKind.WORK, payload={'summary': 'Built backend services.'}, sensitivity=Sensitivity.LOW))


def _seed_job(
    runtime: AppRuntime,
    *,
    source_job_id: str,
    title: str,
    hash_value: str,
    status: JobLifecycleStatus = JobLifecycleStatus.CANDIDATE,
    session=None,
) -> str:
    posting = NormalizedJobPosting(
        company_name='Acme',
        company_key='acme',
        title=title,
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id=source_job_id,
        posting_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        apply_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        location_raw='Remote - United States',
        location_normalized='remote united states',
        country_code='US',
        workplace_type=WorkplaceType.REMOTE,
        employment_type='full_time',
        experience_level=ExperienceLevel.ENTRY_LEVEL,
        description='Backend engineering role.',
        normalized_description='backend engineering role',
        discovered_at=utcnow(),
        job_identity_key=f'identity-{source_job_id}',
        duplicate_cluster_key=f'cluster-{source_job_id}',
        lifecycle_status=status,
        notes={'board': 'acme', 'list_payload_hash': hash_value, 'detail_payload_hash': f'detail-{hash_value}'},
    )

    def seed(active_session) -> str:
        job = JobRepository(active_session).upsert_job(posting)
        job.board_token = 'acme'
        return job.id

    if session is not None:
        return seed(session)
    with runtime.session_scope() as managed_session:
        return seed(managed_session)


def _save_qualification(runtime: AppRuntime, job_id: str, *, score: int, fit: SponsorshipFit = SponsorshipFit.LIKELY_COMPATIBLE, confidence: float = 0.8, reasons: list[str] | None = None) -> None:
    with runtime.session_scope() as session:
        JobRepository(session).save_qualification(
            job_id,
            QualificationResult(
                score=score,
                decision=JobLifecycleStatus.CANDIDATE,
                reasons=reasons or ['matched personal filters'],
                sponsorship_current=SponsorshipSignal.UNKNOWN,
                sponsorship_future=SponsorshipSignal.UNKNOWN,
                cpt_support=SponsorshipSignal.UNKNOWN,
                opt_support=SponsorshipSignal.UNKNOWN,
                fit=fit,
                confidence=confidence,
            ),
        )


def test_compose_personal_query_overrides_daily_defaults(runtime: AppRuntime) -> None:
    _seed_saved_searches(runtime)
    update_personal_preferences(
        runtime.workspace,
        updates={
            'enabled_saved_search_presets': ['backend-core'],
            'countries': ['US'],
            'remote_only': True,
            'experience_levels': [ExperienceLevel.ENTRY_LEVEL],
            'default_result_limit': 25,
            'allow_unknown_compensation': True,
        },
    )
    runtime.config = runtime.config.load(runtime.workspace)
    query = compose_personal_query(JobSearchQuery(title_keywords=['backend'], limit=50), runtime.config.personal)

    assert query.title_keywords == ['backend']
    assert query.countries == ['US']
    assert query.remote_only is True
    assert query.experience_levels == [ExperienceLevel.ENTRY_LEVEL]
    assert query.limit == 25
    assert query.allow_unknown_compensation is True


@pytest.mark.anyio
async def test_run_personal_daily_and_inbox_classify_real_changes(monkeypatch, runtime: AppRuntime) -> None:
    _seed_saved_searches(runtime)
    _seed_profile(runtime)
    update_personal_preferences(
        runtime.workspace,
        updates={
            'enabled_saved_search_presets': ['backend-core', 'frontend-ui'],
            'countries': ['US'],
            'remote_only': True,
            'workplace_types': [WorkplaceType.REMOTE],
            'experience_levels': [ExperienceLevel.ENTRY_LEVEL],
            'default_result_limit': 25,
            'auto_prepare_after_discovery': True,
        },
    )
    runtime.config = runtime.config.load(runtime.workspace)

    approved_job_id = _seed_job(runtime, source_job_id='1', title='Backend Platform Engineer', hash_value='old-1')
    updated_job_id = _seed_job(runtime, source_job_id='2', title='Backend Services Engineer', hash_value='old-2')

    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        approved_application = app_repo.ensure_application(approved_job_id, ApplicationMode.DRY_RUN)
        approved_application.status = JobLifecycleStatus.APPROVED_FOR_SUBMIT
        approved_application.review_status = ReviewStatus.APPROVED

    async def fake_sync(self, query_override=None, board_tokens=None, **kwargs):
        with runtime.session_scope() as session:
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            job_repo = JobRepository(session)
            run = run_repo.create_run('sync', ApplicationMode.DRY_RUN, checkpoint_state={'query': query_override.model_dump(mode='json') if query_override else {}})

            approved_job = job_repo.get_job(approved_job_id)
            updated_job = job_repo.get_job(updated_job_id)
            assert approved_job is not None and updated_job is not None
            approved_job.notes = {**(approved_job.notes or {}), 'list_payload_hash': 'old-1', 'detail_payload_hash': 'detail-old-1'}
            updated_job.notes = {**(updated_job.notes or {}), 'list_payload_hash': 'new-2', 'detail_payload_hash': 'detail-new-2'}
            audit_repo.emit('job.discovered', 'job_posting', approved_job.id, run_id=run.id, payload={'board': 'acme'})
            audit_repo.emit('job.discovered', 'job_posting', updated_job.id, run_id=run.id, payload={'board': 'acme'})

            new_job_id = _seed_job(runtime, source_job_id='3', title='Backend New Role', hash_value='new-3', session=session)
            screened_job_id = _seed_job(runtime, source_job_id='4', title='Support Specialist', hash_value='new-4', status=JobLifecycleStatus.SCREENED_OUT, session=session)
            audit_repo.emit('job.discovered', 'job_posting', new_job_id, run_id=run.id, payload={'board': 'acme'})
            audit_repo.emit('job.discovered', 'job_posting', screened_job_id, run_id=run.id, payload={'board': 'acme'})
            run_repo.complete_run(run.id, status=RunStatus.COMPLETED, checkpoint_state={'jobs_seen': 4})
            return run.id

    async def fake_prepare(self, job_id: str, mode: ApplicationMode = ApplicationMode.DRY_RUN):
        with runtime.session_scope() as session:
            app_repo = ApplicationRepository(session)
            job_repo = JobRepository(session)
            application = app_repo.ensure_application(job_id, mode)
            job = job_repo.get_job(job_id)
            assert job is not None
            if job.source_job_id == '2':
                application.status = JobLifecycleStatus.READY_FOR_REVIEW
                application.review_status = ReviewStatus.PENDING
                job.lifecycle_status = JobLifecycleStatus.READY_FOR_REVIEW
            else:
                application.status = JobLifecycleStatus.NEEDS_USER_INPUT
                application.review_status = ReviewStatus.NEEDS_USER_INPUT
                job.lifecycle_status = JobLifecycleStatus.NEEDS_USER_INPUT
            application.prepared_at = utcnow()
        return f'prepare-{job_id}'

    monkeypatch.setattr(GreenhouseScaleOrchestrator, 'sync_boards', fake_sync)
    monkeypatch.setattr(Orchestrator, 'run_prepare_for_job', fake_prepare)

    summary = await run_personal_daily(runtime)

    assert approved_job_id in summary.matching_job_ids
    assert updated_job_id in summary.updated_job_ids
    assert len(summary.new_job_ids) == 1
    assert len(summary.ready_for_preparation_job_ids) == 2
    assert updated_job_id in summary.added_to_review_job_ids
    assert len(summary.needs_user_input_application_ids) == 1
    assert len(summary.screened_out_job_ids) == 0
    assert summary.query_names_by_job_id[approved_job_id] == ['backend-core']

    latest = latest_personal_daily_summary(runtime)
    assert latest is not None
    assert latest.run_id == summary.run_id

    inbox = build_personal_inbox(runtime, limit=5)
    assert len(inbox.new_matching_jobs) == 1
    assert len(inbox.ready_for_review) == 1
    assert len(inbox.needs_user_input) == 1
    assert len(inbox.approved_pending_submit) == 1
    assert inbox.ready_for_review[0].job_id == updated_job_id
    assert inbox.approved_pending_submit[0].job_id == approved_job_id
    assert inbox.ready_for_review[0].query_names == ['backend-core']


@pytest.mark.anyio
async def test_personal_preview_helpers_use_local_templates(monkeypatch, runtime: AppRuntime, tmp_path: Path) -> None:
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='10', title='Backend Preview Engineer', hash_value='preview-10')
    tex_path = tmp_path / 'resume.tex'
    tex_path.write_text('resume', encoding='utf-8')
    template_path = tmp_path / 'cover_letter_template.json'
    template_path.write_text('{"salutation": "Dear Hiring Team at {company_name},", "paragraphs": ["I am applying for the {job_title} role at {company_name}."], "closing": "Sincerely,", "signature_name": "{name}"}', encoding='utf-8')
    runtime.documents.template_config.resume_renderer = 'latex'
    runtime.documents.template_config.resume_template_path = tex_path
    runtime.documents.template_config.cover_letter_template_path = template_path

    def fake_render_latex(base_name: str, context: dict[str, object]):
        path = runtime.documents.artifacts_dir / f'{base_name}.resume.pdf'
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(kind='pdf', path=path, content_hash=hash_content(path.read_bytes().hex()), validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 32})

    def fake_resume_text(base_name: str, pdf_artifact: RenderedArtifact, context: dict[str, object]):
        path = runtime.documents.artifacts_dir / f'{base_name}.resume.txt'
        path.write_text('Test User\nuser@example.com', encoding='utf-8')
        return RenderedArtifact(kind='resume', path=path, content_hash='resume-text', validation_results={'valid': True, 'text_length': 28, 'contains_placeholder': False, 'missing_contact_fields': []})

    def fake_render_typst(template_name: str, base_name: str, context: dict[str, object]):
        path = runtime.documents.artifacts_dir / f"{base_name}.{template_name.replace('.typ', '')}.pdf"
        path.write_bytes(b'%PDF-1.4\n%stub\n')
        return RenderedArtifact(kind='pdf', path=path, content_hash='typst-hash', validation_results={'valid': True, 'page_count': 1, 'one_page_ok': True, 'contains_placeholder': False, 'missing_contact_fields': [], 'text_length': 64})

    monkeypatch.setattr(runtime.documents, 'render_latex_resume', fake_render_latex)
    monkeypatch.setattr(runtime.documents, 'write_resume_text_from_pdf', fake_resume_text)
    monkeypatch.setattr(runtime.documents, 'render_typst', fake_render_typst)

    resume_preview = await preview_personal_resume(runtime, job_id)
    cover_letter_preview = await preview_personal_cover_letter(runtime, job_id)

    assert resume_preview['job_id'] == job_id
    assert any(artifact['path'].endswith('resume.pdf') for artifact in resume_preview['artifacts'])
    assert any(artifact['path'].endswith('resume.txt') for artifact in resume_preview['artifacts'])
    assert cover_letter_preview['job_id'] == job_id
    assert any(artifact['path'].endswith('cover_letter.pdf') for artifact in cover_letter_preview['artifacts'])


@pytest.mark.anyio
async def test_personal_preview_resume_fails_clearly_when_renderer_is_broken(monkeypatch, runtime: AppRuntime, tmp_path: Path) -> None:
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='11', title='Broken Preview Role', hash_value='preview-11')
    tex_path = tmp_path / 'resume.tex'
    tex_path.write_text('resume', encoding='utf-8')
    runtime.documents.template_config.resume_renderer = 'latex'
    runtime.documents.template_config.resume_template_path = tex_path

    def fake_render_latex(base_name: str, context: dict[str, object]):
        path = runtime.documents.artifacts_dir / f'{base_name}.resume.pdf'
        return RenderedArtifact(kind='pdf', path=path, content_hash='', validation_results={'valid': False, 'failure_reason': 'latex_compile_failed'})

    monkeypatch.setattr(runtime.documents, 'render_latex_resume', fake_render_latex)

    with pytest.raises(ValueError, match='latex_compile_failed'):
        await preview_personal_resume(runtime, job_id)




@pytest.mark.anyio
async def test_personal_triage_actions_are_reversible_and_visible(runtime: AppRuntime) -> None:
    _seed_saved_searches(runtime)
    update_personal_preferences(runtime.workspace, updates={'enabled_saved_search_presets': ['backend-core']})
    runtime.config = runtime.config.load(runtime.workspace)
    job_id = _seed_job(runtime, source_job_id='20', title='Backend Triage Engineer', hash_value='triage-20')
    _save_qualification(runtime, job_id, score=24)

    shortlisted = await shortlist_job(runtime, job_id, reason_code='top_pick', note='follow closely')
    assert shortlisted.decision.status == PersonalTriageStatus.SHORTLISTED

    payload = explain_personal_job(runtime, job_id)
    assert payload.decision.status == PersonalTriageStatus.SHORTLISTED
    assert payload.explanation.triage_status == PersonalTriageStatus.SHORTLISTED

    dismissed = await dismiss_job(
        runtime,
        job_id,
        reason_code='not_interested',
        note='same company and title are noisy',
        suppression_scope=PersonalSuppressionScope.COMPANY_TITLE,
    )
    assert dismissed.decision.status == PersonalTriageStatus.DISMISSED
    assert dismissed.created_rules

    suppressed_payload = explain_personal_job(runtime, job_id)
    assert suppressed_payload.explanation.suppressed is True
    assert any('Job dismissed by operator' in reason for reason in suppressed_payload.explanation.suppression_reasons)

    restored = await unsuppress_job(runtime, job_id, clear_job_status=True, clear_scopes=[PersonalSuppressionScope.COMPANY_TITLE])
    assert restored.decision.status == PersonalTriageStatus.NEW
    assert restored.cleared_rules

    restored_payload = explain_personal_job(runtime, job_id)
    assert restored_payload.explanation.suppressed is False
    assert restored_payload.decision.status == PersonalTriageStatus.NEW


@pytest.mark.anyio
async def test_personal_daily_run_ranks_jobs_and_suppresses_dismissed_noise(monkeypatch, runtime: AppRuntime) -> None:
    _seed_saved_searches(runtime)
    update_personal_preferences(
        runtime.workspace,
        updates={
            'enabled_saved_search_presets': ['backend-core'],
            'countries': ['US'],
            'remote_only': True,
            'workplace_types': [WorkplaceType.REMOTE],
            'compensation_min': 100000,
            'allow_unknown_compensation': True,
        },
    )
    runtime.config = runtime.config.load(runtime.workspace)

    ranked_job_id = _seed_job(runtime, source_job_id='30', title='Backend Platform Engineer', hash_value='rank-30')
    suppressed_job_id = _seed_job(runtime, source_job_id='31', title='Backend Support Engineer', hash_value='rank-31')

    now = utcnow()
    with runtime.session_scope() as session:
        ranked_job = JobRepository(session).get_job(ranked_job_id)
        suppressed_job = JobRepository(session).get_job(suppressed_job_id)
        assert ranked_job is not None and suppressed_job is not None
        ranked_job.posted_at = now
        ranked_job.compensation_min = 160000
        ranked_job.compensation_max = 190000
        ranked_job.compensation_currency = 'USD'
        ranked_job.compensation_interval = 'yearly'
        suppressed_job.posted_at = now.replace(year=now.year - 1)
        suppressed_job.compensation_min = None
        suppressed_job.compensation_max = None
        suppressed_job.compensation_currency = None
        suppressed_job.compensation_interval = None

    _save_qualification(runtime, ranked_job_id, score=45, fit=SponsorshipFit.LIKELY_COMPATIBLE, reasons=['excellent fit'])
    _save_qualification(runtime, suppressed_job_id, score=12, fit=SponsorshipFit.REVIEW_REQUIRED, reasons=['unclear fit'])
    await dismiss_job(runtime, suppressed_job_id, reason_code='noise', suppression_scope=PersonalSuppressionScope.JOB)

    async def fake_sync(self, query_override=None, board_tokens=None, **kwargs):
        with runtime.session_scope() as session:
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            run = run_repo.create_run('sync', ApplicationMode.DRY_RUN, checkpoint_state={'query': query_override.model_dump(mode='json') if query_override else {}})
            audit_repo.emit('job.discovered', 'job_posting', ranked_job_id, run_id=run.id, payload={'board': 'acme'})
            audit_repo.emit('job.discovered', 'job_posting', suppressed_job_id, run_id=run.id, payload={'board': 'acme'})
            run_repo.complete_run(run.id, status=RunStatus.COMPLETED, checkpoint_state={'jobs_seen': 2})
            return run.id

    monkeypatch.setattr(GreenhouseScaleOrchestrator, 'sync_boards', fake_sync)

    summary = await run_personal_daily(runtime)

    assert summary.matching_job_ids == [ranked_job_id]
    assert summary.suppressed_job_ids == [suppressed_job_id]
    assert any('Compensation is disclosed' in reason for reason in summary.explanations_by_job_id[ranked_job_id].ranking_reasons)
    assert summary.explanations_by_job_id[suppressed_job_id].suppressed is True

    inbox = build_personal_inbox(runtime, limit=5)
    assert all(item.job_id != suppressed_job_id for item in inbox.new_matching_jobs)

    inbox_with_suppressed = build_personal_inbox(runtime, limit=5, include_suppressed=True)
    assert any(item.job_id == suppressed_job_id for item in inbox_with_suppressed.suppressed_jobs)


def test_resolve_personal_queries_syncs_builtin_preset_titles(runtime: AppRuntime) -> None:
    update_personal_preferences(runtime.workspace, updates={'enabled_saved_search_presets': ['swe_new_grad_core']})
    runtime.config = runtime.config.load(runtime.workspace)
    with runtime.session_scope() as session:
        SavedSearchRepository(session).save(
            SavedSearch(
                name='swe_new_grad_core',
                query_payload=JobSearchQuery(title_keywords=['software engineer', 'engineer i'], limit=50),
            )
        )

    queries = resolve_personal_queries(runtime)

    assert queries[0].name == 'swe_new_grad_core'
    assert 'software engineer & computer science - recent grad/full time' in queries[0].query.title_keywords
