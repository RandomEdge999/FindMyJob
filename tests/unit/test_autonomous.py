from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.enums import ApplicationMode, FactKind, JobLifecycleStatus, ModelRole, QuestionType, ReviewStatus, Sensitivity
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import ApplicationQuestion, ModelProfile, ProfileFact
from findmyjob.filefirst.models import ApplicationEntry, BoardDiscoveryState, EvaluationResult, FileFact, InboxJob, RunRecord, SubmissionRecord
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.db.repositories import ApplicationRepository, AuditRepository, JobRepository, ProfileRepository, RunRepository
from findmyjob.model_router.router import ModelRouter
from findmyjob.personal.autonomous import approve_question_memory, answer_queued_question, list_question_queue, run_autonomous_tick
from findmyjob.sources.normalizer import build_normalized_job

runner = CliRunner()


def _seed_job(runtime: AppRuntime, *, source_job_id: str, title: str, description: str) -> str:
    posting = build_normalized_job(
        company_name='Acme',
        title=title,
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id=source_job_id,
        posting_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        apply_url=f'https://boards.greenhouse.io/acme/jobs/{source_job_id}',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description=description,
    )
    with runtime.session_scope() as session:
        job = JobRepository(session).upsert_job(posting)
        job.board_token = 'acme'
        return job.id


def _seed_profile(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        repo.upsert_fact(ProfileFact(fact_id='contact.primary', kind=FactKind.CONTACT, payload={'name': 'Test User', 'email': 'user@example.com'}, sensitivity=Sensitivity.LOW))
        repo.upsert_fact(ProfileFact(fact_id='work.primary', kind=FactKind.WORK, payload={'summary': 'Built backend services for internal customers.'}, sensitivity=Sensitivity.LOW))
        repo.upsert_fact(ProfileFact(fact_id='auth.primary', kind=FactKind.AUTHORIZATION, payload={'is_authorized': True, 'requires_future_sponsorship': True}, sensitivity=Sensitivity.MEDIUM))


def _seed_question(runtime: AppRuntime, job_id: str) -> tuple[str, str]:
    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        application = app_repo.ensure_application(job_id, ApplicationMode.AUTO_SUBMIT)
        application.status = JobLifecycleStatus.NEEDS_USER_INPUT
        application.review_status = ReviewStatus.NEEDS_USER_INPUT
        question = app_repo.store_question(
            application.id,
            ApplicationQuestion(
                prompt_text='Are you authorized to work in the United States?',
                normalized_key='auth-work-us',
                question_type=QuestionType.BOOLEAN,
                widget_type='select',
                required=True,
                options=['Yes', 'No'],
            ),
        )
        return application.id, question.id


def _record_sync_discovery(runtime: AppRuntime, job_id: str) -> str:
    with runtime.session_scope() as session:
        run = RunRepository(session).create_run('test_sync', ApplicationMode.DRY_RUN, checkpoint_state={'source': 'test'})
        AuditRepository(session).emit(
            'job.discovered',
            'job_posting',
            job_id,
            run_id=run.id,
            payload={'source': 'test'},
        )
        return run.id


def test_model_router_process_transport_supports_json_generation(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self, stdin: bytes):
            request = json.loads(stdin.decode('utf-8'))
            payload = {
                'ok': True,
                'content': None,
                'json': {
                    'green_light': True,
                    'score': 91,
                    'reasons': [f"role={request['role']}"],
                    'warnings': [],
                    'skip_reason': None,
                },
                'error': None,
            }
            return json.dumps(payload).encode('utf-8'), b''

    async def fake_create_subprocess_exec(*command, **kwargs):
        assert command[0] == 'prism'
        return FakeProcess()

    monkeypatch.setattr('findmyjob.model_router.router.asyncio.create_subprocess_exec', fake_create_subprocess_exec)
    profile = ModelProfile(
        name='prism-classifier',
        role=ModelRole.CLASSIFIER,
        provider='prism',
        model='prism-local',
        transport='process',
        command=['prism', 'classify'],
        working_dir=str(tmp_path),
    )
    router = ModelRouter(config=AppRuntime.bootstrap(tmp_path).config.model_copy(update={'models': {'prism-classifier': profile}}))

    payload, profile_name = anyio.run(router.generate_json_with_profile, ModelRole.CLASSIFIER, 'classify this job')

    assert profile_name == 'prism-classifier'
    assert payload['green_light'] is True
    assert payload['score'] == 91


def test_model_router_process_transport_rejects_invalid_stdout(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self, stdin: bytes):
            return b'not-json', b''

    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeProcess()

    monkeypatch.setattr('findmyjob.model_router.router.asyncio.create_subprocess_exec', fake_create_subprocess_exec)
    profile = ModelProfile(
        name='prism-classifier',
        role=ModelRole.CLASSIFIER,
        provider='prism',
        model='prism-local',
        transport='process',
        command=['prism', 'classify'],
        working_dir=str(tmp_path),
    )
    router = ModelRouter(config=AppRuntime.bootstrap(tmp_path).config.model_copy(update={'models': {'prism-classifier': profile}}))

    with pytest.raises(RuntimeError, match='invalid JSON'):
        anyio.run(router.generate_json_with_profile, ModelRole.CLASSIFIER, 'classify this job')


def test_model_router_remote_json_generation_parses_code_fenced_payload(tmp_path: Path, monkeypatch) -> None:
    async def fake_chat_completion(self, profile, payload, *, mode):
        assert mode == 'json'
        return {
            'choices': [
                {
                    'message': {
                        'content': '```json\n{"resume_draft": {"headline": "Backend Engineer", "summary_lines": ["Built Python services."], "selected_work_fact_ids": [], "selected_project_fact_ids": [], "selected_skill_fact_ids": [], "custom_bullets": []}, "cover_letter_draft": {"salutation": "Hello", "paragraphs": ["p1", "p2", "p3", "p4"], "closing": "Thanks", "signature_name": "Test User"}}\n```'
                    }
                }
            ]
        }

    monkeypatch.setattr(ModelRouter, '_chat_completion', fake_chat_completion)
    profile = ModelProfile(
        name='lmstudio-writer',
        role=ModelRole.WRITER,
        provider='lmstudio',
        model='lmstudio-community/Qwen3-8B-GGUF',
        local=True,
        transport='local_http',
        base_url='http://127.0.0.1:1234',
    )
    router = ModelRouter(config=AppRuntime.bootstrap(tmp_path).config.model_copy(update={'models': {'lmstudio-writer': profile}}))

    payload, profile_name = anyio.run(router.generate_json_with_profile, ModelRole.WRITER, 'draft this job')

    assert profile_name == 'lmstudio-writer'
    assert payload['resume_draft']['headline'] == 'Backend Engineer'
    assert payload['cover_letter_draft']['paragraphs'] == ['p1', 'p2', 'p3', 'p4']


def test_model_router_remote_json_generation_accepts_trailing_chatter(tmp_path: Path, monkeypatch) -> None:
    async def fake_chat_completion(self, profile, payload, *, mode):
        assert mode == 'json'
        return {
            'choices': [
                {
                    'message': {
                        'content': '{"score": 4.0, "grade": "B"}\nI hope this helps.'
                    }
                }
            ]
        }

    monkeypatch.setattr(ModelRouter, '_chat_completion', fake_chat_completion)
    profile = ModelProfile(
        name='lmstudio-classifier',
        role=ModelRole.CLASSIFIER,
        provider='lmstudio',
        model='smollm3-3b',
        local=True,
        transport='local_http',
        base_url='http://127.0.0.1:1234',
    )
    router = ModelRouter(config=AppRuntime.bootstrap(tmp_path).config.model_copy(update={'models': {'lmstudio-classifier': profile}}))

    payload, profile_name = anyio.run(router.generate_json_with_profile, ModelRole.CLASSIFIER, 'classify this job')

    assert profile_name == 'lmstudio-classifier'
    assert payload['score'] == 4.0
    assert payload['grade'] == 'B'


def test_questions_queue_answer_and_memory_cli(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='question-1', title='Backend Engineer', description='Backend role.')
    application_id, question_id = _seed_question(runtime, job_id)

    queued = runner.invoke(app, ['questions', 'queue', '--json', '--workspace', str(tmp_path)])
    assert queued.exit_code == 0, queued.output
    assert question_id in queued.output

    answered = runner.invoke(
        app,
        ['questions', 'answer', application_id, question_id, '--answer', 'Yes', '--json', '--workspace', str(tmp_path)],
    )
    assert answered.exit_code == 0, answered.output
    assert '"existing_answer": "Yes"' in answered.output

    approved = runner.invoke(
        app,
        ['questions', 'approve-memory', application_id, question_id, '--json', '--workspace', str(tmp_path)],
    )
    assert approved.exit_code == 0, approved.output
    assert '"has_approved_memory": true' in approved.output


def test_question_queue_functions_store_answer_memory(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='question-2', title='Backend Engineer', description='Backend role.')
    application_id, question_id = _seed_question(runtime, job_id)

    queue = list_question_queue(runtime)
    assert len(queue) == 1
    assert queue[0].question_id == question_id

    updated = answer_queued_question(runtime, application_id, question_id, 'Yes')
    assert updated.existing_answer == 'Yes'

    approved = approve_question_memory(runtime, application_id, question_id)
    assert approved.has_approved_memory is True

    with runtime.session_scope() as session:
        app_repo = ApplicationRepository(session)
        memories = app_repo.find_answer_memory('auth-work-us', {'question_type': 'boolean', 'source_adapter': 'greenhouse', 'option_signature': 'no|yes'})
        assert len(memories) == 1
        assert memories[0].answer_text == 'Yes'


@pytest.mark.anyio
async def test_autonomous_tick_skips_hard_gated_job(monkeypatch, tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    runtime.config.autonomous.enabled = True
    runtime.config.autonomous.use_personal_presets = False
    runtime.config.autonomous.min_submit_delay_seconds = 0
    runtime.config.autonomous.max_submit_delay_seconds = 0
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='auto-1', title='Security Engineer', description='US citizens only. Active security clearance required.')

    async def fake_sync(self, query_override=None, board_tokens=None, **kwargs):
        return _record_sync_discovery(runtime, job_id)

    async def should_not_prepare(self, job_id: str, mode: ApplicationMode = ApplicationMode.AUTO_SUBMIT):
        raise AssertionError('hard-gated job should not reach prepare')

    monkeypatch.setattr('findmyjob.personal.autonomous.GreenhouseScaleOrchestrator.sync_boards', fake_sync)
    monkeypatch.setattr('findmyjob.personal.autonomous.Orchestrator.run_prepare_for_job', should_not_prepare)

    summary = await run_autonomous_tick(runtime)

    assert job_id in summary.skipped_job_ids
    assert summary.decision_by_job_id[job_id].hard_gate_passed is False
    assert 'requires_us_citizen_or_national' in summary.decision_by_job_id[job_id].hard_gate_reasons
    assert not summary.prepared_application_ids
    assert not summary.submitted_application_ids


@pytest.mark.anyio
async def test_autonomous_tick_submits_greenlit_job_and_exports_ledger(monkeypatch, tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    runtime.config.autonomous.enabled = True
    runtime.config.autonomous.use_personal_presets = False
    runtime.config.autonomous.min_submit_delay_seconds = 0
    runtime.config.autonomous.max_submit_delay_seconds = 0
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='auto-2', title='Backend Platform Engineer', description='Build backend systems for product teams across the United States.')

    async def fake_sync(self, query_override=None, board_tokens=None, **kwargs):
        return _record_sync_discovery(runtime, job_id)

    async def fake_generate_json_with_profile(self, role, prompt, system_prompt=None):
        if role == ModelRole.CLASSIFIER:
            return ({'green_light': True, 'score': 95, 'reasons': ['strong backend fit'], 'warnings': [], 'skip_reason': None}, 'classifier')
        if role == ModelRole.WRITER:
            return ({
                'resume_draft': {
                    'headline': 'Entry-level backend engineer',
                    'summary_lines': ['Built backend services with Python.'],
                    'selected_work_fact_ids': ['work.primary'],
                    'selected_project_fact_ids': [],
                    'selected_skill_fact_ids': [],
                    'custom_bullets': ['Improved service reliability for internal users.'],
                },
                'cover_letter_draft': {
                    'salutation': 'Dear Hiring Team at Acme,',
                    'paragraphs': ['I am applying for the Backend Platform Engineer role at Acme.'],
                    'closing': 'Sincerely,',
                    'signature_name': 'Test User',
                },
            }, 'writer')
        if role == ModelRole.VERIFIER:
            return ({'approved': True, 'issues': []}, 'verifier')
        raise AssertionError(f'unexpected role: {role}')

    async def fake_prepare(self, target_job_id: str, mode: ApplicationMode = ApplicationMode.AUTO_SUBMIT):
        with runtime.session_scope() as session:
            app_repo = ApplicationRepository(session)
            application = app_repo.ensure_application(target_job_id, mode)
            application.status = JobLifecycleStatus.READY_FOR_REVIEW
            application.review_status = ReviewStatus.PENDING
        return f'prepare-{target_job_id}'

    async def fake_review(self, application_id: str, action: ReviewStatus, reason: str | None = None):
        with runtime.session_scope() as session:
            application = ApplicationRepository(session).get_application(application_id)
            assert application is not None
            application.review_status = action
            application.status = JobLifecycleStatus.APPROVED_FOR_SUBMIT
        return JobLifecycleStatus.APPROVED_FOR_SUBMIT.value

    async def fake_apply(self, application_id: str, mode: ApplicationMode = ApplicationMode.AUTO_SUBMIT):
        with runtime.session_scope() as session:
            app_repo = ApplicationRepository(session)
            application = app_repo.get_application(application_id)
            assert application is not None
            application.status = JobLifecycleStatus.SUBMITTED
            app_repo.record_submit_attempt(application.id, JobLifecycleStatus.SUBMITTED.value, runtime.config.policy.default_source_policy, {'evidence': {'failure_reason': None}})
        return f'apply-{application_id}'

    async def fake_inspect(self, application_id: str):
        return {'submission_status': JobLifecycleStatus.SUBMITTED.value, 'failure_reason': None}

    monkeypatch.setattr('findmyjob.personal.autonomous.GreenhouseScaleOrchestrator.sync_boards', fake_sync)
    monkeypatch.setattr('findmyjob.model_router.router.ModelRouter.generate_json_with_profile', fake_generate_json_with_profile)
    monkeypatch.setattr('findmyjob.personal.autonomous.Orchestrator.run_prepare_for_job', fake_prepare)
    monkeypatch.setattr('findmyjob.personal.autonomous.Orchestrator.review_action', fake_review)
    monkeypatch.setattr('findmyjob.personal.autonomous.Orchestrator.run_apply_for_application', fake_apply)
    monkeypatch.setattr('findmyjob.personal.autonomous.Orchestrator.inspect_submission_result', fake_inspect)

    summary = await run_autonomous_tick(runtime)

    assert len(summary.submitted_application_ids) == 1
    assert job_id in summary.decision_by_job_id

    csv_path = runtime.config.autonomous_ledger_output_path(runtime.workspace).with_suffix('.csv')
    assert csv_path.exists()
    csv_text = csv_path.read_text(encoding='utf-8')
    assert 'matched_presets' in csv_text
    assert 'ai_greenlight' in csv_text
    assert 'submit_outcome' in csv_text

    applications_csv = runtime.config.autonomous_ledger_output_path(runtime.workspace).parent / 'applications.csv'
    questions_csv = runtime.config.autonomous_ledger_output_path(runtime.workspace).parent / 'questions.csv'
    accounts_csv = runtime.config.autonomous_ledger_output_path(runtime.workspace).parent / 'accounts.csv'
    assert applications_csv.exists()
    assert questions_csv.exists()
    assert accounts_csv.exists()
    assert 'application_id' in applications_csv.read_text(encoding='utf-8')
    assert 'question_id' in questions_csv.read_text(encoding='utf-8')
    assert 'login_email' in accounts_csv.read_text(encoding='utf-8')



def test_questions_queue_cli_renders_table_without_schema_error(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    _seed_profile(runtime)
    job_id = _seed_job(runtime, source_job_id='question-render', title='Backend Engineer', description='Backend role.')
    _seed_question(runtime, job_id)

    queued = runner.invoke(app, ['questions', 'queue', '--workspace', str(tmp_path)])

    assert queued.exit_code == 0, queued.output
    assert 'Question Queue' in queued.output
    assert 'Are you authorized to work in the United States?' in queued.output


def test_db_reset_operational_clears_runtime_state_and_preserves_memory(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv("# Test User\n\nResume body.\n")
    ws.save_facts(
        [
            FileFact(fact_id="contact.primary", kind="contact", payload={"name": "Test User", "email": "user@example.com"}),
            FileFact(fact_id="authorization.primary", kind="authorization", payload={"is_authorized": True}),
        ]
    )
    ws.save_answer_memory([])

    job = InboxJob(
        job_id="job-reset",
        company="Acme",
        company_key="acme",
        title="Backend Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="reset-1",
        url="https://boards.greenhouse.io/acme/jobs/reset-1",
        apply_url="https://boards.greenhouse.io/acme/jobs/reset-1",
        location="Remote - United States",
        description="Backend role.",
        workflow_state="pdf_ready",
        board_family="greenhouse",
        automation_tier="preview_first",
        job_identity_key="job-reset",
        duplicate_cluster_key="acme-backend-engineer",
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    ws.save_evaluation(
        EvaluationResult(
            job_id="job-reset",
            company="Acme",
            role="Backend Engineer",
            source="greenhouse",
            url=job.url,
            score=4.2,
            grade="A",
            summary="Strong fit.",
            keywords=["python"],
            fit_reasons=["Python background"],
            gaps=[],
            report_markdown="# Evaluation",
            resume_headline="Backend Engineer",
            resume_summary_lines=["Shipped backend systems."],
        )
    )
    report_path = ws.report_path_for("001", "Acme", "2026-04-10")
    report_path.write_text("# Evaluation\n", encoding="utf-8")
    pdf_path = ws.resume_pdf_path_for("001", "Acme", "2026-04-10")
    pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")
    ws.upsert_application(
        ApplicationEntry(
            id="001",
            job_id="job-reset",
            date="2026-04-10",
            company="Acme",
            role="Backend Engineer",
            score=4.2,
            grade="A",
            status="Needs Review",
            pdf=True,
            report=ws.relative_path(report_path),
            url=job.url,
            source="greenhouse",
        )
    )
    ws.save_submission(
        SubmissionRecord(
            application_id="001",
            job_id="job-reset",
            company="Acme",
            role="Backend Engineer",
            source="greenhouse",
            apply_url=job.apply_url,
            status="needs_review",
            event_status="needs_review",
            submit_ready=False,
            questions=[],
            missing_required_fields=[],
        )
    )
    ws.save_run(RunRecord(run_id="run-reset", run_type="discover", status="completed"))
    discovery = BoardDiscoveryState()
    discovery.sources["greenhouse"].boards = ["datadog"]
    ws.save_board_discovery_state(discovery)
    trace_ref = ws.write_live_trace("run-reset", category="model-calls", name="screen-step", payload={"prompt": "hello"})
    assert (tmp_path / trace_ref).exists()

    result = runner.invoke(app, ['db', 'reset-operational', '--json', '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['reset'] is True
    assert payload['deleted']['applications'] == 1
    assert payload['deleted']['jobs'] == 1
    assert payload['deleted']['evaluations'] == 1
    assert payload['deleted']['submissions'] == 1
    assert payload['deleted']['runs'] == 1
    assert payload['deleted']['live_run_traces'] >= 1
    assert payload['preserved']['facts'] == 'profile/facts.yml'
    assert payload['preserved']['answer_memory'] == 'profile/answer-memory.yml'
    assert payload['preserved']['cv'] == 'cv.md'
    assert payload['preserved']['basic_profile_local_override'] == '.fmj/local-overrides/filefirst/user-profile.yml'
    assert payload['ledger_exports']['configured_output_base'] == '.fmj/exports/ledger'
    assert payload['history_after_reset']['run_history'] is False
    assert payload['history_after_reset']['existing_ledger_exports'] is True

    assert ws.load_inbox() == []
    assert ws.load_applications() == []
    assert ws.load_job("job-reset") is None
    assert ws.load_evaluation("job-reset") is None
    assert ws.find_submission("001") is None
    assert ws.load_runs() == []
    assert ws.load_board_discovery_state().sources["greenhouse"].boards == []
    assert len(ws.load_facts()) == 2
    assert ws.load_cv().startswith("# Test User")
