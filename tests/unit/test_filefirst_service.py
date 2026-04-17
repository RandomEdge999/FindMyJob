from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio

from findmyjob.core.enums import JobLifecycleStatus
from findmyjob.core.types import FormFieldBinding, SubmissionEvidence, SubmissionPlan, SubmissionResult
from findmyjob.filefirst.models import AnswerMemoryEntry, ApplicationEntry, EvaluationResult, FileFact, InboxJob, LiveRunState, RunRecord, ScreeningDecision, SubmissionQuestion, SubmissionRecord
from findmyjob.filefirst.service import FileFirstOperatorService
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.grounding.service import GroundingService
from findmyjob.sources.contracts import ExtractionResult, FormFieldContract


class _FakeGrounding:
    async def answer_question(self, question, facts, *, options=None, normalized_key=None, answer_memory=None, memory_context=None, allow_sensitive_fallback=True):
        _ = facts
        _ = options
        _ = normalized_key
        _ = answer_memory
        _ = memory_context
        _ = allow_sensitive_fallback
        from findmyjob.core.enums import QuestionType, VerificationStatus
        from findmyjob.core.types import GroundedAnswer

        return GroundedAnswer(
            question=question,
            question_type=QuestionType.DATE,
            answer='2026-05-01',
            confidence=1.0,
            reason='test',
            verification_status=VerificationStatus.VERIFIED,
        )


class _FakeAdapter:
    async def load_application_contract(self, client, posting):
        _ = client
        _ = posting
        return ExtractionResult(
            questions=[
                FormFieldContract(
                    name='start_date',
                    prompt_text='What is your preferred start date?',
                    field_type='date',
                    widget_type='date',
                    required=True,
                    normalized_key='preferred-start-date',
                ).to_question()
            ]
        )

    def bind_answers(self, posting, question_answers, artifacts_by_kind):
        _ = posting
        _ = question_answers
        _ = artifacts_by_kind
        return SubmissionPlan(source_kind='greenhouse', application_url='https://boards.greenhouse.io/acme/jobs/100')


def _seed_service_workspace(tmp_path: Path) -> tuple[FileFirstOperatorService, FileWorkspace]:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n')
    ws.save_facts([
        FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
        FileFact(fact_id='work.primary', kind='work', payload={'summary': 'Built local automation tooling.'}),
    ])
    job = InboxJob(
        job_id='job-100',
        company='Acme',
        company_key='acme',
        title='Backend Platform Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='100',
        url='https://boards.greenhouse.io/acme/jobs/100',
        apply_url='https://boards.greenhouse.io/acme/jobs/100',
        location='Remote',
        description='Build local AI workflows.',
        workflow_state='pdf_ready',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-100',
        duplicate_cluster_key='acme-backend-platform-engineer',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    evaluation = EvaluationResult(job_id='job-100', company='Acme', role='Backend Platform Engineer', source='greenhouse', url=job.url, summary='Strong fit.', score=4.5, grade='A')
    ws.save_evaluation(evaluation)
    report_path = ws.report_path_for('001', 'Acme', '2026-04-05')
    report_path.write_text('# Evaluation\n', encoding='utf-8')
    ws.resume_pdf_path_for('001', 'Acme', '2026-04-05').write_bytes(b'%PDF-1.4\n%stub\n')
    ws.upsert_application(ApplicationEntry(id='001', job_id='job-100', date='2026-04-05', company='Acme', role='Backend Platform Engineer', score=4.5, grade='A', status='PDF Ready', pdf=True, report=ws.relative_path(report_path), url=job.url, source='greenhouse'))
    return FileFirstOperatorService(tmp_path), ws


def test_prepare_submission_reuses_manual_answers_from_disk(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            manual_answers={'preferred-start-date': '2026-06-01'},
        )
    )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._adapter_for_job', lambda self, job: _FakeAdapter())
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._grounding_service', lambda self: _FakeGrounding())

    record = anyio.run(service._prepare_submission_async, '001', 'run-1')

    assert record.submit_ready is True
    assert record.questions[0].existing_answer == '2026-06-01'
    assert ws.load_submission('001').manual_answers['preferred-start-date'] == '2026-06-01'


def test_review_application_can_record_manual_submission(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            event_status='needs_user_input',
            submit_ready=False,
            missing_required_fields=['What is your preferred start date?'],
        )
    )

    result = service.review_application(application_id='001', action='mark_submitted')
    updated_submission = ws.load_submission('001')
    updated_application = ws.find_application('001')
    inbox_job = next(item for item in ws.load_inbox() if item.job_id == 'job-100')

    assert result['manual_submitted'] is True
    assert result['status'] == 'submitted'
    assert updated_submission is not None
    assert updated_submission.status == 'submitted'
    assert updated_submission.submitted_at is not None
    assert updated_submission.result['manual_confirmation'] is True
    assert updated_submission.result['review_history'][-1]['type'] == 'review.action.mark_submitted'
    assert updated_application is not None
    assert updated_application.status == 'Applied'
    assert inbox_job.workflow_state == 'applied'
    assert service.review_queue_payload()['count'] == 0


def test_review_payloads_expose_summary_artifacts_and_manual_handoff(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            questions=[
                SubmissionQuestion(
                    question_id='q1',
                    prompt_text='What is your preferred start date?',
                    normalized_key='preferred-start-date',
                    question_type='date',
                    widget_type='date',
                    required=True,
                    needs_user_input=True,
                )
            ],
            missing_required_fields=['What is your preferred start date?'],
            warnings=['Manual handoff active'],
            result={'manual_handoff_watch': {'active': True, 'status': 'watching', 'pending_count': 2}},
        )
    )

    queue = service.review_queue_payload()
    detail = service.application_detail_payload('001')
    item = next(entry for entry in queue['items'] if entry['application_id'] == '001')

    assert item['review_summary']['severity'] == 'danger'
    assert item['review_summary']['blocker_count'] == 1
    assert item['review_summary']['next_action'] == 'sync_manual_input'
    assert item['manual_handoff']['active'] is True
    assert item['manual_handoff']['pending_count'] == 2
    assert detail['summary']['next_action'] == 'sync_manual_input'
    assert any(artifact['kind'] == 'resume_pdf' for artifact in detail['artifacts'])
    assert any(artifact['kind'] == 'evaluation_report' for artifact in detail['artifacts'])
    assert detail['history'] == []


def test_manual_answer_memory_appends_alternates_instead_of_replacing(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_answer_memory(
        [
            AnswerMemoryEntry(
                canonical_question='are-you-a-veteran-have-you-served-in-the-military',
                context_constraints={},
                answer_text='I am not a protected veteran',
                approved=True,
            )
        ]
    )
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            event_status='needs_user_input',
            questions=[
                SubmissionQuestion(
                    question_id='veteran-status',
                    prompt_text='Are you a veteran/have you served in the military?',
                    normalized_key='are-you-a-veteran-have-you-served-in-the-military',
                    question_type='sensitive',
                    widget_type='select',
                    required=True,
                    options=['Active duty', 'Military spouse', 'No military service'],
                    needs_user_input=True,
                )
            ],
            missing_required_fields=['Are you a veteran/have you served in the military?'],
        )
    )

    service.answer_question(
        application_id='001',
        question_id='veteran-status',
        answer_text='No military service',
        approve_memory=True,
        auto_retry=False,
    )

    answers = [
        item for item in ws.load_answer_memory()
        if item.canonical_question == 'are-you-a-veteran-have-you-served-in-the-military'
    ]

    assert len(answers) == 2
    assert {item.answer_text for item in answers} == {
        'I am not a protected veteran',
        'No military service',
    }
    assert any(item.context_constraints.get('option_signature') == 'active duty|military spouse|no military service' for item in answers)


def test_manual_handoff_preview_reports_when_browser_cannot_stay_open(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    prepared_record = SubmissionRecord(
        application_id='001',
        job_id='job-100',
        company='Acme',
        role='Backend Platform Engineer',
        source='greenhouse',
        status='needs_user_input',
        plan=SubmissionPlan(source_kind='greenhouse', application_url='https://boards.greenhouse.io/acme/jobs/100').model_dump(mode='json'),
    )
    ws.save_submission(prepared_record)

    class _PreviewAdapter:
        async def preview_submission(self, job, plan, output_dir, *, keep_browser_open=False):
            _ = (job, plan, output_dir, keep_browser_open)
            return SubmissionResult(
                status=JobLifecycleStatus.READY_FOR_REVIEW,
                submitted=False,
                evidence=SubmissionEvidence(
                    final_url='https://boards.greenhouse.io/acme/jobs/100#application',
                    browser_left_open=False,
                ),
            )

    async def fake_prepare(self, application_id, run_id):
        _ = self
        assert application_id == '001'
        assert run_id == 'run-manual'
        return prepared_record

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._adapter_for_job', lambda self, job: _PreviewAdapter())
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    left_open = anyio.run(service._open_manual_handoff_preview_async, '001', 'run-manual')

    assert left_open is False
    saved = ws.load_submission('001')
    assert saved is not None
    assert any('could not be kept open automatically' in note for note in saved.notes)


def test_manual_handoff_preview_rebuilds_submission_plan_before_opening(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    stale_record = SubmissionRecord(
        application_id='001',
        job_id='job-100',
        company='Acme',
        role='Backend Platform Engineer',
        source='greenhouse',
        status='needs_user_input',
        plan=SubmissionPlan(
            source_kind='greenhouse',
            application_url='https://boards.greenhouse.io/acme/jobs/100',
            fields=[],
        ).model_dump(mode='json'),
    )
    fresh_record = stale_record.model_copy(
        update={
            'plan': SubmissionPlan(
                source_kind='greenhouse',
                application_url='https://boards.greenhouse.io/acme/jobs/100',
                fields=[
                    FormFieldBinding(
                        source_field_name='first_name',
                        widget_type='text',
                        prompt_text='First Name',
                        required=True,
                        value='Jamie Lee',
                    )
                ],
            ).model_dump(mode='json')
        }
    )
    ws.save_submission(stale_record)
    prepared_calls: list[tuple[str, str | None]] = []
    seen_field_counts: list[int] = []

    class _PreviewAdapter:
        async def preview_submission(self, job, plan, output_dir, *, keep_browser_open=False):
            _ = (job, output_dir, keep_browser_open)
            seen_field_counts.append(len(plan.fields))
            return SubmissionResult(
                status=JobLifecycleStatus.READY_FOR_REVIEW,
                submitted=False,
                evidence=SubmissionEvidence(browser_left_open=True),
            )

    async def fake_prepare(self, application_id, run_id):
        _ = self
        prepared_calls.append((application_id, run_id))
        ws.save_submission(fresh_record)
        return fresh_record

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._adapter_for_job', lambda self, job: _PreviewAdapter())
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._start_manual_handoff_watcher', lambda self, application_id: True)

    left_open = anyio.run(service._open_manual_handoff_preview_async, '001', 'run-refresh')

    assert left_open is True
    assert prepared_calls == [('001', 'run-refresh')]
    assert seen_field_counts == [1]


def test_manual_handoff_sync_saves_blank_and_corrected_answers(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            questions=[
                SubmissionQuestion(
                    question_id='citizenship',
                    prompt_text='In which country/region do you have citizenship?',
                    normalized_key='country-of-citizenship',
                    question_type='text',
                    widget_type='text',
                    required=True,
                    existing_answer='',
                    needs_user_input=True,
                ),
                SubmissionQuestion(
                    question_id='relocation',
                    prompt_text='Are you open to relocation?',
                    normalized_key='open-to-relocation',
                    question_type='select',
                    widget_type='select',
                    required=True,
                    options=['Yes', 'No'],
                    option_details=[{'label': 'Yes', 'value': 'Yes'}, {'label': 'No', 'value': 'No'}],
                    existing_answer='No',
                ),
                SubmissionQuestion(
                    question_id='email_verification_code',
                    prompt_text='Enter the 8-character verification code sent to your email to continue submission.',
                    normalized_key='email_verification_code',
                    question_type='text',
                    widget_type='text',
                    required=True,
                    existing_answer='',
                    needs_user_input=True,
                ),
            ],
            missing_required_fields=['In which country/region do you have citizenship?'],
            result={'manual_handoff_watch': {'active': True, 'status': 'watching'}},
        )
    )

    async def fake_capture(self, application_id):
        _ = self
        assert application_id == '001'
        return {
            'application_id': application_id,
            'page_found': True,
            'page_url': 'https://boards.greenhouse.io/acme/jobs/100#application',
            'answers': [
                {
                    'question_id': 'citizenship',
                    'prompt_text': 'In which country/region do you have citizenship?',
                    'widget_type': 'text',
                    'answer_text': 'Pakistan',
                },
                {
                    'question_id': 'relocation',
                    'prompt_text': 'Are you open to relocation?',
                    'widget_type': 'select',
                    'answer_text': 'Yes',
                },
                {
                    'question_id': 'email_verification_code',
                    'prompt_text': 'Enter the 8-character verification code sent to your email to continue submission.',
                    'widget_type': 'text',
                    'answer_text': 'KyLB26gG',
                },
            ],
            'error': None,
        }

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._capture_manual_handoff_answers_async', fake_capture)

    result = anyio.run(service._sync_manual_handoff_answers_async, '001', True, 'manual_handoff_test')

    saved = ws.load_submission('001')
    assert saved is not None
    assert result['updated_count'] == 2
    assert result['filled_blank_count'] == 1
    assert result['corrected_answer_count'] == 1
    assert saved.manual_answers['citizenship'] == 'Pakistan'
    assert saved.manual_answers['relocation'] == 'Yes'
    assert 'email_verification_code' not in saved.manual_answers
    assert next(item for item in saved.questions if item.question_id == 'citizenship').needs_user_input is False
    assert next(item for item in saved.questions if item.question_id == 'relocation').needs_user_input is False
    assert next(item for item in saved.questions if item.question_id == 'email_verification_code').needs_user_input is True
    assert saved.result['manual_handoff_watch']['synced_question_count'] == 2
    assert saved.result['manual_handoff_watch']['filled_blank_count'] == 1
    assert saved.result['manual_handoff_watch']['corrected_answer_count'] == 1
    assert saved.result['manual_handoff_watch']['recent_answers'][0]['previous_answer'] == ''
    assert saved.result['manual_handoff_watch']['recent_answers'][1]['change_type'] == 'corrected_answer'
    assert saved.result['review_history'][-1]['type'] == 'review.manual_handoff.synced'

    answer_memory = ws.load_answer_memory()
    assert any(item.canonical_question == 'country-of-citizenship' and item.answer_text == 'Pakistan' for item in answer_memory)
    assert any(item.canonical_question == 'open-to-relocation' and item.answer_text == 'Yes' for item in answer_memory)
    assert not any(item.canonical_question == 'email_verification_code' for item in answer_memory)


def test_answer_question_does_not_remember_email_verification_code(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            questions=[
                SubmissionQuestion(
                    question_id='email_verification_code',
                    prompt_text='Enter the 8-character verification code sent to your email to continue submission.',
                    normalized_key='email_verification_code',
                    question_type='text',
                    widget_type='text',
                    required=True,
                    needs_user_input=True,
                    verification_status='needs_user_input',
                )
            ],
            missing_required_fields=['Enter the 8-character verification code sent to your email to continue submission.'],
        )
    )

    result = service.answer_question(
        application_id='001',
        question_id='email_verification_code',
        answer_text='KyLB26gG',
        approve_memory=True,
        auto_retry=False,
    )

    saved = ws.load_submission('001')
    assert saved is not None
    assert 'email_verification_code' not in saved.manual_answers
    question = next(item for item in saved.questions if item.question_id == 'email_verification_code')
    assert question.existing_answer is None
    assert question.needs_user_input is True
    assert question.confidence_reason == 'transient_answer_not_persisted'
    assert not any(item.canonical_question == 'email_verification_code' for item in ws.load_answer_memory())
    assert result['question']['existing_answer'] is None


def test_email_verification_binding_never_reuses_saved_code(tmp_path: Path) -> None:
    service, _ws = _seed_service_workspace(tmp_path)
    record = SubmissionRecord(
        application_id='001',
        job_id='job-100',
        company='Acme',
        role='Backend Platform Engineer',
        source='greenhouse',
        manual_answers={'email_verification_code': 'KyLB26gG'},
    )
    plan = SubmissionPlan(
        source_kind='greenhouse',
        application_url='https://boards.greenhouse.io/acme/jobs/100',
        fields=[],
    )

    updated_plan = service._with_email_verification_code_binding(plan, record)

    assert updated_plan.fields == []


def test_review_application_can_sync_manual_input(monkeypatch, tmp_path: Path) -> None:
    service, _ws = _seed_service_workspace(tmp_path)

    async def fake_sync(self, application_id, approve_memory=True, source='manual_handoff_sync'):
        _ = self
        assert application_id == '001'
        assert approve_memory is True
        assert source == 'manual_handoff_console_sync'
        return {
            'application_id': application_id,
            'page_found': True,
            'updated_count': 2,
            'filled_blank_count': 1,
            'corrected_answer_count': 1,
            'remaining_blockers': [],
            'status': 'needs_user_input',
            'watch_state': {'active': True, 'status': 'watching'},
        }

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._sync_manual_handoff_answers_async', fake_sync)

    result = service.review_application(application_id='001', action='sync_manual_input')

    assert result['page_found'] is True
    assert result['synced_count'] == 2
    assert result['filled_blank_count'] == 1
    assert result['corrected_answer_count'] == 1
    assert result['remaining_blockers'] == []


def test_submission_registry_persists_across_service_instances(tmp_path: Path) -> None:
    service, _ws = _seed_service_workspace(tmp_path)

    service._remember_submission('001', '2026-04-10T00:00:00Z')

    reloaded = FileFirstOperatorService(tmp_path)

    assert reloaded._is_already_submitted('001') is True
    assert reloaded._submission_registry_path.exists()


def test_submission_registry_ignores_corrupt_json(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    registry_path = ws.fmj_dir / 'submission_registry.json'
    registry_path.write_text('{not valid json', encoding='utf-8')

    service = FileFirstOperatorService(tmp_path)

    assert service._submission_registry == {}


def test_reset_operational_preserves_handled_job_memory(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='submitted',
            submit_ready=False,
        )
    )

    payload = service.reset_operational_state_payload()

    handled = ws.load_handled_jobs()

    assert ws.load_applications() == []
    assert ws.load_submissions() == []
    assert 'job-100' in handled['job_ids']
    assert 'https://boards.greenhouse.io/acme/jobs/100' in handled['urls']
    assert {'company': 'acme', 'role': 'backend platform engineer'} in handled['pairs']
    assert 'acme-backend-platform-engineer' in handled['duplicate_clusters']
    assert payload['handled_jobs']['job_ids'] >= 1


def test_grounding_facts_include_candidate_location_when_profile_facts_do_not(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.candidate.location = 'Austin, TX, US'
    ws.save_profile(profile)

    application = ws.find_application('001')
    job = ws.load_job('job-100')
    assert application is not None
    assert job is not None

    facts = service._grounding_facts_for_application(application, job)
    async def _answer():
        return await GroundingService().answer_question(
            'Which U.S. State or Canadian Province do you reside in?',
            facts,
            options=['Alabama', 'Oklahoma', 'Texas'],
        )

    answer = anyio.run(_answer)

    assert answer.answer == 'Texas'
    location_fact = next(fact for fact in facts if getattr(fact.kind, 'value', fact.kind) == 'location')
    assert location_fact.payload['city'] == 'Austin'
    assert location_fact.payload['region_code'] == 'TX'
    assert location_fact.payload['region'] == 'Texas'
    assert location_fact.payload['country_code'] == 'US'


def test_service_init_does_not_interrupt_running_manual_submission(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_live_state(
        LiveRunState(
            run_id='manual',
            run_type='submission',
            status='running',
            stage='submit',
            started_at='2026-04-13T19:45:35+00:00',
            run_started_at='2026-04-13T19:45:35+00:00',
            latest_operator_message='Submitting Plaid / Technical Support Engineer.',
        )
    )

    _service = FileFirstOperatorService(tmp_path)
    state = ws.load_live_state()

    assert state.status == 'running'
    assert state.run_type == 'submission'


def test_screened_out_pipeline_event_is_not_marked_blocked(monkeypatch, tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    job = InboxJob(
        job_id='job-1',
        company='Acme',
        company_key='acme',
        title='Engineering Manager',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='1',
        url='https://boards.greenhouse.io/acme/jobs/1',
        apply_url='https://boards.greenhouse.io/acme/jobs/1',
        description='Management role.',
        workflow_state='pending',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-1',
        duplicate_cluster_key='job-1',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    service = FileFirstOperatorService(tmp_path)

    screened = job.model_copy(
        update={
            'workflow_state': 'screened_out',
            'screening': ScreeningDecision(
                approved=False,
                reasons=['Title contains manager.'],
                confidence=0.95,
                status='rejected',
            ),
        }
    )

    monkeypatch.setattr('findmyjob.filefirst.service.screen_job', lambda workspace, job_id: (screened, screened.screening))

    result = service._run_pipeline_with_events(run_id='run-1', run_type='autonomous', approved_limit=5)

    live_state = ws.load_live_state()
    last_event = ws.load_live_events(limit=1)[0]
    assert result['screened_out']
    assert live_state.status == 'running'
    assert last_event.event_type == 'autonomous.screening.completed'
    assert last_event.status == 'completed'


def test_autonomous_pipeline_step_returns_counts_on_success_without_failed_jobs(monkeypatch, tmp_path: Path) -> None:
    service, _ws = _seed_service_workspace(tmp_path)

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._run_pipeline_with_events',
        lambda self, *, run_id, run_type, approved_limit: {
            'evaluated': [{'application_id': '001'}],
            'pdfs': [{'application_id': '001'}],
            'failed_jobs': [],
        },
    )

    result = service._autonomous_pipeline_step(run_id='auto-1', notes=[])

    assert result == {'stop': False, 'terminal_error': None, 'evaluated': 1, 'drafted': 1, 'blocked_for_chatgpt': False}


def test_autonomous_pipeline_step_blocks_on_global_chatgpt_browser_failure(monkeypatch, tmp_path: Path) -> None:
    service, _ws = _seed_service_workspace(tmp_path)

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._run_pipeline_with_events',
        lambda self, *, run_id, run_type, approved_limit: {
            'evaluated': [{'application_id': '005'}],
            'pdfs': [{'application_id': '005', 'success': False}],
            'failed_jobs': [
                {
                    'application_id': '005',
                    'job_id': 'job-100',
                    'company': 'Acme',
                    'role': 'Backend Platform Engineer',
                    'stage': 'drafting',
                    'error': 'chatgpt_http_431:ChatGPT is returning HTTP 431 in the dedicated browser session.',
                }
            ],
        },
    )

    result = service._autonomous_pipeline_step(run_id='auto-1', notes=[])

    assert result['stop'] is True
    assert result['blocked_for_chatgpt'] is True
    assert 'chatgpt_http_431' in str(result['terminal_error'])


def test_serial_chatgpt_mode_forces_single_draft_batch_and_apply_threshold(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.ready_to_apply_threshold = 10
    ws.save_profile(profile)

    service.save_chatgpt_drafting_settings({"max_parallel_jobs": 1})

    assert service._serial_chatgpt_mode() is True
    assert service._draft_batch_size_target() == 1
    assert service._ready_to_apply_threshold() == 1
    payload = service.autonomous_status_payload()
    assert payload["drafting_mode"] == "serial"
    assert payload["configured_ready_to_apply_threshold"] == 10
    assert payload["ready_to_apply_threshold"] == 1


def test_live_status_payload_reflects_active_chatgpt_drafting(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_live_state(
        LiveRunState(
            run_id='auto-1',
            run_type='autonomous',
            status='running',
            stage='screening',
            current_company='Acme',
            current_role='Backend Platform Engineer',
            current_title='Acme / Backend Platform Engineer',
            event_count=1,
        )
    )

    monkeypatch.setattr(
        'findmyjob.filefirst.service.ChatGPTDraftingService.status_payload',
        lambda self: {
            'progress': {
                'status': 'running',
                'phase': 'waiting_for_markers',
                'application_id': '001',
                'job_id': 'job-100',
                'company': 'Figma',
                'role': 'Data Platform Engineer',
                'last_observation': 'ChatGPT is still thinking; markers have not appeared yet.',
            },
            'batch': {
                'member_count': 10,
                'completed_count': 4,
                'failed_count': 1,
                'active_worker_count': 5,
                'handoff_status': 'waiting_for_batch',
            },
        },
    )

    payload = service.live_status_payload(limit=5)

    assert payload['state']['status'] == 'running'
    assert payload['state']['stage'] == 'drafting'
    assert payload['state']['active_application_id'] == '001'
    assert payload['state']['current_title'] == 'Figma / Data Platform Engineer'
    assert payload['state']['latest_operator_message'] == 'ChatGPT is still thinking; markers have not appeared yet.'
    assert payload['drafting']['batch']['member_count'] == 10


def test_run_autonomous_persists_run_history_without_sqlite(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.ready_to_apply_threshold = 1
    ws.save_profile(profile)

    def fake_scan(self, *, run_id, run_type, limit=50):
        _ = self
        _ = run_id
        _ = run_type
        _ = limit
        return {'targets': {'greenhouse': ['acme']}, 'discovered': 1, 'new_jobs': 0, 'updated_jobs': 0, 'duplicates': 0, 'saved_job_ids': []}

    async def fake_prepare(self, application_id, run_id):
        _ = run_id
        record = SubmissionRecord(application_id=application_id, job_id='job-100', company='Acme', role='Backend Platform Engineer', source='greenhouse', status='ready_for_submit', submit_ready=True)
        ws.save_submission(record)
        application = ws.find_application(application_id)
        assert application is not None
        ws.upsert_application(application.model_copy(update={'status': 'Ready to Submit'}))
        return record

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._run_discovery_scan', fake_scan)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)
    # Patch _continue_after_ready (called synchronously, returns result of _submit_application_async)
    def fake_continue_after_ready(self, application_id, run_id):
        _ = run_id
        return SubmissionRecord(application_id=application_id, job_id='job-100', company='Acme', role='Backend Platform Engineer', source='greenhouse', status='submitted', submit_ready=False)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fake_continue_after_ready)

    # FIX: Seed the workspace with an actual ApplicationEntry so run_autonomous has candidates to process.
    # The real run_pipeline/create_application creates entries, but our monkeypatch doesn't, so we create it manually.
    entry = ApplicationEntry(
        id='001',
        job_id='job-100',
        date='2026-04-06',
        company='Acme',
        role='Backend Platform Engineer',
        score=4.0,
        grade='B',
        status='Ready to Submit',
        pdf=True,
        report='reports/001-acme-2026-04-06.md',
        url='https://boards.greenhouse.io/acme/jobs/100',
        source='greenhouse',
    )
    ws.upsert_application(entry)

    result = service.run_autonomous()

    assert result['started'] is True
    assert result['submitted_application_ids'] == ['001']
    runs = ws.load_runs()
    assert runs
    assert runs[0].run_type == 'autonomous'
    assert runs[0].submitted_application_ids == ['001']


def test_run_autonomous_starts_apply_once_ready_queue_reaches_threshold(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.per_company_daily_cap = 20
    profile.runtime.automation.ready_to_apply_threshold = 10
    ws.save_profile(profile)

    original = ws.find_application('001')
    assert original is not None
    ws.upsert_application(original.model_copy(update={'status': 'Applied'}))

    for index in range(10):
        app_id = f"{index + 10:03d}"
        job_id = f"job-{index + 200}"
        company = f"Acme {index}"
        job = InboxJob(
            job_id=job_id,
            company=company,
            company_key=f"acme-{index}",
            title="Backend Platform Engineer",
            source="greenhouse",
            source_kind="greenhouse",
            source_job_id=str(index + 200),
            url=f"https://boards.greenhouse.io/acme/jobs/{index + 200}",
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{index + 200}",
            description="Build local AI workflows.",
            workflow_state="pdf_ready",
            board_family="greenhouse",
            automation_tier="auto_submit_supported",
            job_identity_key=job_id,
            duplicate_cluster_key=job_id,
            screening=ScreeningDecision(approved=True, reasons=["fit"], confidence=0.9),
        )
        ws.save_job(job)
        ws.upsert_inbox_jobs([job])
        ws.upsert_application(
            ApplicationEntry(
                id=app_id,
                job_id=job_id,
                date="2026-04-06",
                company=company,
                role="Backend Platform Engineer",
                score=4.5,
                grade="A",
                status="Ready to Submit",
                pdf=True,
                report=f"reports/{app_id}-{company.casefold().replace(' ', '-')}.md",
                url=job.url,
                source="greenhouse",
            )
        )
        ws.save_submission(
            SubmissionRecord(
                application_id=app_id,
                job_id=job_id,
                company=company,
                role="Backend Platform Engineer",
                source="greenhouse",
                status="ready_for_submit",
                event_status="ready_for_submit",
                submit_ready=True,
            )
        )

    actions: list[str] = []

    def fail_discovery(self, *, run_id, run_type, limit=50):
        _ = (self, run_id, run_type, limit)
        actions.append("discover")
        raise AssertionError("discovery should not run before apply when threshold is met")

    def fake_continue_after_ready(self, application_id, run_id):
        _ = run_id
        actions.append(f"apply:{application_id}")
        record = ws.load_submission(application_id)
        assert record is not None
        ws.save_submission(record.model_copy(update={"status": "submitted", "submit_ready": False, "submitted_at": "2026-04-06T00:00:00+00:00"}))
        application = ws.find_application(application_id)
        assert application is not None
        ws.upsert_application(application.model_copy(update={"status": "Applied"}))
        return ws.load_submission(application_id)

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._run_discovery_scan', fail_discovery)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fake_continue_after_ready)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    result = service.run_autonomous()

    assert actions
    assert actions[0].startswith("apply:")
    assert result["submitted_application_ids"]


def test_run_autonomous_stops_when_persisted_daily_cap_is_already_reached(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    fixed_now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.daily_submit_cap = 1
    profile.runtime.automation.per_company_daily_cap = 20
    profile.runtime.automation.ready_to_apply_threshold = 1
    ws.save_profile(profile)
    ws.save_submission(
        SubmissionRecord(
            application_id='submitted-today',
            job_id='job-submitted-today',
            company='Prior Co',
            role='Platform Engineer',
            source='greenhouse',
            status='submitted',
            submit_ready=False,
            submitted_at='2026-04-15T15:30:00+00:00',
        )
    )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._local_now', staticmethod(lambda: fixed_now))

    def fail_continue(self, application_id, run_id):
        _ = (self, application_id, run_id)
        raise AssertionError('apply should not run after the daily cap is already reached')

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fail_continue)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    result = service.run_autonomous()

    assert result['submitted_application_ids'] == []
    assert any('Daily submit cap reached (1)' in note for note in result['notes'])


def test_run_autonomous_ignores_previous_day_submissions_for_daily_caps(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    fixed_now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.daily_submit_cap = 1
    profile.runtime.automation.per_company_daily_cap = 1
    profile.runtime.automation.ready_to_apply_threshold = 1
    ws.save_profile(profile)
    ws.save_submission(
        SubmissionRecord(
            application_id='submitted-yesterday',
            job_id='job-submitted-yesterday',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='submitted',
            submit_ready=False,
            submitted_at='2026-04-14T15:30:00+00:00',
        )
    )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._local_now', staticmethod(lambda: fixed_now))

    async def fake_prepare(self, application_id, run_id):
        _ = (self, run_id)
        record = SubmissionRecord(
            application_id=application_id,
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            event_status='ready_for_submit',
            submit_ready=True,
        )
        ws.save_submission(record)
        application = ws.find_application(application_id)
        assert application is not None
        ws.upsert_application(application.model_copy(update={'status': 'Ready to Submit'}))
        return record

    def fake_continue_after_ready(self, application_id, run_id):
        _ = (self, run_id)
        record = ws.load_submission(application_id)
        assert record is not None
        updated = record.model_copy(update={'status': 'submitted', 'submit_ready': False, 'submitted_at': '2026-04-15T17:00:00+00:00'})
        ws.save_submission(updated)
        application = ws.find_application(application_id)
        assert application is not None
        ws.upsert_application(application.model_copy(update={'status': 'Applied'}))
        return updated

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fake_continue_after_ready)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    result = service.run_autonomous()

    assert result['submitted_application_ids'] == ['001']


def test_autonomous_status_payload_reports_daily_submit_progress(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    fixed_now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    profile = ws.load_profile()
    profile.runtime.automation.daily_submit_cap = 5
    ws.save_profile(profile)
    ws.save_submission(
        SubmissionRecord(
            application_id='submitted-today',
            job_id='job-submitted-today',
            company='Prior Co',
            role='Platform Engineer',
            source='greenhouse',
            status='submitted',
            submit_ready=False,
            submitted_at='2026-04-15T16:00:00+00:00',
        )
    )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._local_now', staticmethod(lambda: fixed_now))

    payload = service.autonomous_status_payload()

    assert payload['daily_submit_cap'] == 5
    assert payload['daily_submitted_today'] == 1
    assert payload['daily_remaining_capacity'] == 4
    assert payload['daily_submit_day'] == '2026-04-15'


def test_run_autonomous_finishes_blocked_when_chatgpt_browser_is_unhealthy(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    job = ws.load_job('job-100')
    assert job is not None
    ws.save_job(job.model_copy(update={'workflow_state': 'pending'}))
    ws.upsert_inbox_jobs([ws.load_job('job-100')])

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._autonomous_pipeline_step',
        lambda self, *, run_id, notes: {
            'stop': True,
            'terminal_error': 'chatgpt_http_431:ChatGPT is returning HTTP 431 in the dedicated browser session.',
            'evaluated': 1,
            'drafted': 0,
            'blocked_for_chatgpt': True,
        },
    )
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._applications_requiring_prepare', lambda self: [])
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._ready_to_apply_applications', lambda self: [])
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    result = service.run_autonomous()

    assert result['blocked_for_chatgpt'] is True
    runs = ws.load_runs()
    assert runs
    assert runs[0].status == 'blocked'


def test_run_autonomous_applies_one_threshold_batch_before_refreshing_discovery(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.per_company_daily_cap = 20
    profile.runtime.automation.ready_to_apply_threshold = 5
    ws.save_profile(profile)

    original = ws.find_application('001')
    assert original is not None
    ws.upsert_application(original.model_copy(update={'status': 'Applied'}))

    for index in range(7):
        app_id = f"{index + 10:03d}"
        job_id = f"job-{index + 300}"
        company = f"Batch {index}"
        job = InboxJob(
            job_id=job_id,
            company=company,
            company_key=f"batch-{index}",
            title="Backend Platform Engineer",
            source="greenhouse",
            source_kind="greenhouse",
            source_job_id=str(index + 300),
            url=f"https://boards.greenhouse.io/acme/jobs/{index + 300}",
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{index + 300}",
            description="Build local AI workflows.",
            workflow_state="pdf_ready",
            board_family="greenhouse",
            automation_tier="auto_submit_supported",
            job_identity_key=job_id,
            duplicate_cluster_key=job_id,
            screening=ScreeningDecision(approved=True, reasons=["fit"], confidence=0.9),
        )
        ws.save_job(job)
        ws.upsert_inbox_jobs([job])
        ws.upsert_application(
            ApplicationEntry(
                id=app_id,
                job_id=job_id,
                date="2026-04-06",
                company=company,
                role="Backend Platform Engineer",
                score=4.5,
                grade="A",
                status="Ready to Submit",
                pdf=True,
                report=f"reports/{app_id}-{company.casefold().replace(' ', '-')}.md",
                url=job.url,
                source="greenhouse",
            )
        )
        ws.save_submission(
            SubmissionRecord(
                application_id=app_id,
                job_id=job_id,
                company=company,
                role="Backend Platform Engineer",
                source="greenhouse",
                status="ready_for_submit",
                event_status="ready_for_submit",
                submit_ready=True,
            )
        )

    actions: list[str] = []

    def fake_continue_after_ready(self, application_id, run_id):
        _ = run_id
        actions.append(f"apply:{application_id}")
        record = ws.load_submission(application_id)
        assert record is not None
        ws.save_submission(record.model_copy(update={"status": "submitted", "submit_ready": False, "submitted_at": "2026-04-06T00:00:00+00:00"}))
        application = ws.find_application(application_id)
        assert application is not None
        ws.upsert_application(application.model_copy(update={"status": "Applied"}))
        return ws.load_submission(application_id)

    def fake_discovery_step(self, *, run_id, notes):
        _ = (self, run_id, notes)
        actions.append("discover")
        return {"stop": True, "terminal_error": None, "discovery_exhausted": True, "new_jobs": 0}

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fake_continue_after_ready)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._autonomous_discovery_step', fake_discovery_step)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    service.run_autonomous()

    assert actions[:5] == [f"apply:{app_id}" for app_id in ("010", "011", "012", "013", "014")]
    assert actions[5] == "discover"


def test_autonomous_discovery_step_scans_enough_candidates_to_fill_batch(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.ready_to_apply_threshold = 5
    ws.save_profile(profile)

    seen: list[int] = []

    def fake_scan(self, *, run_id, run_type, limit=50):
        _ = (self, run_id, run_type)
        seen.append(limit)
        return {
            'targets': {'greenhouse': ['acme']},
            'seed_summary': {'crawled_pages': 0, 'errors': [], 'unsupported_urls': 0, 'board_targets': {}},
            'discovered': 0,
            'new_jobs': 0,
            'updated_jobs': 0,
            'duplicates': 0,
            'saved_job_ids': [],
            'eligible_job_ids': [],
            'skipped_job_ids': [],
            'errors': [],
        }

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._run_discovery_scan', fake_scan)

    result = service._autonomous_discovery_step(run_id='auto-test', notes=[])

    assert result['discovery_exhausted'] is True
    assert seen == [50]

def test_start_launch_rehearsal_scans_and_screens_jobs(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    fresh_job = InboxJob(
        job_id='job-200',
        company='Beta',
        company_key='beta',
        title='New Grad Backend Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        url='https://boards.greenhouse.io/beta/jobs/200',
        apply_url='https://boards.greenhouse.io/beta/jobs/200',
        location='Remote',
        description='Entry-level backend role.',
        workflow_state='pending',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        ats_family='greenhouse',
        ats_preview_supported=True,
        rehearsal_eligible=True,
        rehearsal_rank=128.0,
        discovery_method='live_market:greenhouse',
        job_identity_key='job-200',
        duplicate_cluster_key='beta-new-grad-backend-engineer',
    )
    ws.save_job(fresh_job)
    ws.upsert_inbox_jobs([fresh_job])

    async def fake_discover_live_market(workspace, limit=5, candidate_limit=None):
        _ = workspace
        _ = limit
        _ = candidate_limit
        return {
            'targets': {'greenhouse': ['beta']},
            'seed_summary': {'crawled_pages': 0, 'errors': [], 'unsupported_urls': 0, 'board_targets': {}},
            'discovered': 1,
            'new_jobs': 1,
            'updated_jobs': 0,
            'duplicates': 0,
            'saved_job_ids': ['job-200'],
            'eligible_job_ids': ['job-200'],
            'skipped_job_ids': [],
            'errors': [],
        }

    def fake_screen(workspace, target, force=False):
        _ = workspace
        _ = force
        job = ws.load_job(target)
        assert job is not None
        screened = job.model_copy(update={'screening': ScreeningDecision(approved=True, reasons=['Good launch fit.'], confidence=0.84), 'workflow_state': 'pending'})
        ws.save_job(screened)
        ws.upsert_inbox_jobs([screened])
        return screened, screened.screening

    monkeypatch.setattr('findmyjob.filefirst.service.discover_live_market', fake_discover_live_market)
    monkeypatch.setattr('findmyjob.filefirst.service.screen_job', fake_screen)

    result = service.start_launch_rehearsal(limit=1)

    assert result['suggested_job_id'] == 'job-200'
    assert result['scan']['eligible_job_ids'] == ['job-200']
    assert result['screened_jobs'][0]['screening']['status'] == 'approved'
    assert result['screened_jobs'][0]['rehearsal_eligible'] is True
    assert ws.load_runs()[0].run_type == 'rehearsal_scan'


def test_run_launch_rehearsal_persists_preview_ready(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    job = ws.load_job('job-100')
    assert job is not None
    screened = job.model_copy(update={'screening': ScreeningDecision(approved=True, reasons=['Entry-level fit.'], confidence=0.91), 'workflow_state': 'pending'})
    ws.save_job(screened)
    ws.upsert_inbox_jobs([screened])
    resume_text = ws.output_dir / 'rehearsal.resume.txt'
    cover_text = ws.output_dir / 'rehearsal.cover_letter.txt'
    resume_text.write_text('Resume body without placeholders.\n', encoding='utf-8')
    cover_text.write_text('Cover letter body without placeholders.\n', encoding='utf-8')

    monkeypatch.setattr(
        'findmyjob.filefirst.service.evaluate_target',
        lambda workspace, target: {'application_id': '001', 'job_id': 'job-100', 'company': 'Acme', 'role': 'Backend Platform Engineer', 'report_path': 'reports/001-acme-2026-04-05.md'},
    )
    monkeypatch.setattr(
        'findmyjob.filefirst.service.build_pdf_for_target',
        lambda workspace, target: {
            'job_id': 'job-100',
            'application_id': '001',
            'renderer': 'typst',
            'template_bridge_used': False,
            'resume_template_path': None,
            'cover_letter_template_path': '.fmj/local_templates/cover_letter_template.json',
            'pdf_path': 'output/cv-001-acme-2026-04-05.pdf',
            'cover_letter_path': 'output/cover-letter-001-acme.pdf',
            'resume_text_path': str(resume_text.relative_to(ws.root)).replace('\\', '/'),
            'cover_letter_text_path': str(cover_text.relative_to(ws.root)).replace('\\', '/'),
            'warnings': [],
            'render_error': None,
        },
    )

    async def fake_prepare(self, application_id, run_id):
        _ = run_id
        return SubmissionRecord(
            application_id=application_id,
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            submit_ready=True,
            plan={'source_kind': 'greenhouse', 'application_url': 'https://boards.greenhouse.io/acme/jobs/100'},
        )

    async def fake_preview(self, application_id, run_id):
        _ = run_id
        return SubmissionRecord(
            application_id=application_id,
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='preview_ready',
            submit_ready=False,
            preview_ready=True,
        )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._preview_application_async', fake_preview)

    result = service.run_launch_rehearsal(job_id='job-100')

    assert result['ready_to_review'] is True
    assert result['artifact_issues'] == []
    assert ws.load_runs()[0].run_type == 'rehearsal'


def test_run_launch_rehearsal_reuses_saved_evaluation(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    job = ws.load_job('job-100')
    assert job is not None
    screened = job.model_copy(update={'screening': ScreeningDecision(approved=True, reasons=['Entry-level fit.'], confidence=0.91), 'workflow_state': 'evaluated'})
    ws.save_job(screened)
    ws.upsert_inbox_jobs([screened])
    ws.save_evaluation(
        EvaluationResult(
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            url='https://boards.greenhouse.io/acme/jobs/100',
            score=4.4,
            grade='B',
            report_markdown='# Evaluation\n',
        )
    )

    def _unexpected_eval(workspace, target):
        raise AssertionError(f'evaluate_target should not be called for cached rehearsal target {target}')

    monkeypatch.setattr('findmyjob.filefirst.service.evaluate_target', _unexpected_eval)
    monkeypatch.setattr(
        'findmyjob.filefirst.service.build_pdf_for_target',
        lambda workspace, target: {
            'job_id': 'job-100',
            'application_id': '001',
            'renderer': 'typst',
            'template_bridge_used': False,
            'resume_template_path': None,
            'cover_letter_template_path': '.fmj/local_templates/cover_letter_template.json',
            'pdf_path': 'output/cv-001-acme-2026-04-05.pdf',
            'cover_letter_path': 'output/cover-letter-001-acme.pdf',
            'resume_text_path': 'output/rehearsal.resume.txt',
            'cover_letter_text_path': 'output/rehearsal.cover_letter.txt',
            'warnings': [],
            'render_error': None,
        },
    )

    async def fake_prepare(self, application_id, run_id):
        _ = run_id
        return SubmissionRecord(
            application_id=application_id,
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            submit_ready=True,
            plan={'source_kind': 'greenhouse', 'application_url': 'https://boards.greenhouse.io/acme/jobs/100'},
        )

    async def fake_preview(self, application_id, run_id):
        _ = run_id
        return SubmissionRecord(
            application_id=application_id,
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='preview_ready',
            submit_ready=False,
            preview_ready=True,
        )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._preview_application_async', fake_preview)

    result = service.run_launch_rehearsal(job_id='job-100')

    assert result['evaluation']['score'] == 4.4
    assert result['evaluation']['reused_saved_evaluation'] is True
    assert result['ready_to_review'] is True



def test_prepare_submission_ignores_artifact_companions_and_verified_zero_confidence(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)

    class _Adapter:
        async def load_application_contract(self, client, posting):
            _ = client
            _ = posting
            return ExtractionResult(
                questions=[
                    FormFieldContract(
                        name='resume_text',
                        prompt_text='Resume/CV',
                        field_type='textarea',
                        widget_type='textarea',
                        required=True,
                        normalized_key='resume-cv',
                    ).to_question(),
                    FormFieldContract(
                        name='email',
                        prompt_text='Email',
                        field_type='text',
                        widget_type='text',
                        required=True,
                        normalized_key='email',
                    ).to_question(),
                ]
            )

        def bind_answers(self, posting, question_answers, artifacts_by_kind):
            _ = posting
            _ = question_answers
            _ = artifacts_by_kind
            return SubmissionPlan(source_kind='greenhouse', application_url='https://boards.greenhouse.io/acme/jobs/100')

    class _Grounding:
        async def answer_question(self, question, facts, *, options=None, normalized_key=None, answer_memory=None, memory_context=None, allow_sensitive_fallback=True):
            _ = facts
            _ = options
            _ = normalized_key
            _ = answer_memory
            _ = memory_context
            _ = allow_sensitive_fallback
            from findmyjob.core.enums import QuestionType, VerificationStatus
            from findmyjob.core.types import GroundedAnswer

            if question == 'Resume/CV':
                return GroundedAnswer(
                    question=question,
                    question_type=QuestionType.NARRATIVE,
                    answer=None,
                    confidence=0.0,
                    reason='artifact_companion',
                    verification_status=VerificationStatus.NEEDS_USER_INPUT,
                )
            return GroundedAnswer(
                question=question,
                question_type=QuestionType.DETERMINISTIC,
                answer='user@example.com',
                confidence=0.0,
                reason='fact_rule',
                verification_status=VerificationStatus.VERIFIED,
            )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._adapter_for_job', lambda self, job: _Adapter())
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._grounding_service', lambda self: _Grounding())

    record = anyio.run(service._prepare_submission_async, '001', 'run-2')

    assert record.submit_ready is True
    assert record.missing_required_fields == []
    assert record.ungrounded_answers == []
    assert record.low_confidence_answers == []

def test_question_queue_ignores_rejected_records(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    application = ws.find_application('001')
    assert application is not None

    ws.upsert_application(application.model_copy(update={'status': 'Rejected'}))
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='rejected',
            event_status='rejected',
            submit_ready=False,
            questions=[
                SubmissionQuestion(
                    question_id='preferred-start-date',
                    prompt_text='What is your preferred start date?',
                    normalized_key='preferred-start-date',
                    question_type='date',
                    widget_type='date',
                    required=True,
                    needs_user_input=True,
                )
            ],
        )
    )

    queue = service.question_queue_payload(limit=20)

    assert queue['count'] == 0
    assert service.autonomous_status_payload()['unresolved_prompts'] == 0


def test_save_autonomous_settings_syncs_portal_enablement(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)

    result = service.save_autonomous_settings(
        {
            'enabled': True,
            'submit_enabled': True,
            'default_submit_mode': 'auto_submit',
            'ready_to_apply_threshold': 12,
            'browser_attach_enabled': False,
            'browser_cdp_url': 'http://127.0.0.1:9666',
            'browser_mode': 'headless',
            'max_open_tabs': 6,
            'daily_submit_cap': 25,
            'per_company_daily_cap': 2,
            'production_sources': ['greenhouse', 'lever', 'ashby'],
            'captcha_strategy': 'manual',
            'captcha_provider': 'capmonster',
            'captcha_api_key_env': 'FMJ_CAPTCHA_KEY',
            'captcha_solve_timeout_seconds': 240,
        }
    )

    portals = ws.load_portals()
    assert result['saved'] is True
    assert result['portal_sources'] == {'greenhouse': True, 'lever': True, 'ashby': True}
    assert result['autonomous']['captcha_strategy'] == 'manual'
    assert result['autonomous']['captcha_strategy_effective'] == 'manual'
    assert result['captcha']['captcha_provider'] == 'capmonster'
    assert ws.load_profile().runtime.automation.production_sources == ['greenhouse', 'lever', 'ashby']
    assert ws.load_profile().runtime.automation.ready_to_apply_threshold == 12
    assert ws.load_profile().runtime.automation.browser_cdp_url == 'http://127.0.0.1:9666'
    assert portals.sources['greenhouse'].enabled is True
    assert portals.sources['lever'].enabled is True
    assert portals.sources['ashby'].enabled is True
    workspace_config = ws.workspace_config_path.read_text(encoding='utf-8')
    assert 'default_application_mode = "auto_submit"' in workspace_config
    assert 'require_human_review_for_submit = false' in workspace_config
    assert workspace_config.count('submit_enabled = true') >= 3
    assert 'browser_cdp_url = "http://127.0.0.1:9666"' in workspace_config
    assert 'strategy = "manual"' in workspace_config
    assert 'provider = "capmonster"' in workspace_config
    assert 'api_key_env = "FMJ_CAPTCHA_KEY"' in workspace_config


def test_normalized_job_includes_effective_captcha_settings(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)

    normalized = service._normalized_job(ws.load_job('job-100'))

    assert normalized.notes['captcha_strategy'] == 'manual'
    assert normalized.notes['captcha_provider'] == '2captcha'
    assert normalized.notes['captcha_api_key_env'] == 'CAPTCHA_API_KEY'
    assert normalized.notes['captcha_solve_timeout'] == 300


def test_autonomous_discovery_batch_size_fills_real_draft_batch(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.ready_to_apply_threshold = 10
    ws.save_profile(profile)

    assert service._autonomous_discovery_batch_size() == 100


def test_pending_job_priority_prefers_technical_non_senior_titles(tmp_path: Path) -> None:
    service, _ws = _seed_service_workspace(tmp_path)
    strong = type('Job', (), {'title': 'Software Engineer, Backend', 'rehearsal_rank': 100.0, 'discovered_at': '2026-04-14T00:00:00+00:00'})()
    weak = type('Job', (), {'title': 'Senior Account Executive', 'rehearsal_rank': 100.0, 'discovered_at': '2026-04-14T00:00:00+00:00'})()

    assert service._pending_job_priority(strong) > service._pending_job_priority(weak)


def test_run_pipeline_emits_drafting_queued_before_actual_started(monkeypatch, tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    job = InboxJob(
        job_id='job-1',
        company='Acme',
        company_key='acme',
        title='Software Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='1',
        url='https://boards.greenhouse.io/acme/jobs/1',
        apply_url='https://boards.greenhouse.io/acme/jobs/1',
        description='Build software.',
        workflow_state='pending',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-1',
        duplicate_cluster_key='acme-software-engineer',
    )
    ws.save_job(job)
    ws.upsert_inbox_jobs([job])
    service = FileFirstOperatorService(tmp_path)
    emitted: list[str] = []

    screened = job.model_copy(
        update={
            'workflow_state': 'screened',
            'screening': ScreeningDecision(
                approved=True,
                reasons=['Strong fit'],
                confidence=0.95,
                status='approved',
            ),
        }
    )

    monkeypatch.setattr('findmyjob.filefirst.service.screen_job', lambda workspace, job_id: (screened, screened.screening))
    monkeypatch.setattr(
        'findmyjob.filefirst.service.evaluate_target',
        lambda workspace, job_id: {
            'application_id': '001',
            'job_id': job_id,
            'company': 'Acme',
            'role': 'Software Engineer',
            'report_path': 'reports/001.md',
        },
    )
    monkeypatch.setattr(
        'findmyjob.filefirst.service.build_pdf_for_target',
        lambda workspace_root, job_id: {
            'application_id': '001',
            'job_id': job_id,
            'pdf_path': 'output/cv-001-acme.pdf',
            'cover_letter_path': 'output/cover-letter-001-acme.pdf',
            'render_error': None,
            'draft': {'writer_profile': 'chatgpt'},
        },
    )
    monkeypatch.setattr(service, '_emit_runtime_event', lambda **kwargs: emitted.append(str(kwargs.get('event_type') or '')))

    result = service._run_pipeline_with_events(run_id='run-1', run_type='autonomous', approved_limit=1)

    assert result['pdfs']
    assert 'autonomous.drafting.queued' in emitted
    assert 'autonomous.drafting.started' in emitted
    assert emitted.index('autonomous.drafting.queued') < emitted.index('autonomous.drafting.started')


def test_autonomous_status_payload_prefers_live_stats_during_active_run(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)

    ws.save_live_state(
        LiveRunState(
            run_id='auto-live',
            run_type='autonomous',
            status='running',
            stage='discovery',
            stats={
                'discovered': 12,
                'screened_out': 4,
                'evaluated': 3,
                'drafted': 1,
                'ready_to_apply': 2,
                'blocked_by_questions': 1,
                'pending_questions': 5,
                'submitted': 0,
                'source_metrics': {
                    'greenhouse': {'boards_scanned': 2, 'jobs_discovered': 11, 'eligible_jobs': 7, 'rejected_jobs': 4, 'errors': 0, 'zero_result': False},
                    'lever': {'boards_scanned': 1, 'jobs_discovered': 6, 'eligible_jobs': 4, 'rejected_jobs': 2, 'errors': 0, 'zero_result': False},
                    'ashby': {'boards_scanned': 1, 'jobs_discovered': 5, 'eligible_jobs': 1, 'rejected_jobs': 4, 'errors': 1, 'zero_result': False},
                },
                'source_warnings': ['ashby reported 1 discovery error(s).'],
                'zero_result_sources': [],
            },
        )
    )

    payload = service.autonomous_status_payload()

    assert payload['discovered'] == 12
    assert payload['screened_out'] == 4
    assert payload['evaluated'] == 3
    assert payload['drafted'] == 1
    assert payload['ready_to_apply'] == 2
    assert payload['blocked_by_questions'] == 1
    assert payload['unresolved_prompts'] == 5
    assert payload['source_metrics']['greenhouse']['boards_scanned'] == 2
    assert payload['source_warnings'] == ['ashby reported 1 discovery error(s).']


def test_autonomous_status_payload_counts_submitted_outside_active_queue(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    application = ws.find_application('001')
    assert application is not None
    ws.upsert_application(application.model_copy(update={'status': 'Applied'}))
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='submitted',
            submit_ready=False,
        )
    )

    payload = service.autonomous_status_payload()

    assert payload['submitted'] == 1


def test_autonomous_status_payload_prefers_fresher_live_summary_for_same_run_id(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_run(
        RunRecord(
            run_id='auto-live',
            run_type='autonomous',
            status='blocked',
            event_status='blocked',
            completed_at='2026-04-14T05:26:57+00:00',
            notes=['001: blocked for manual input'],
        )
    )
    ws.save_live_state(
        LiveRunState(
            run_id='auto-live',
            run_type='submission',
            status='completed',
            stage='submit',
            started_at='2026-04-14T05:48:50+00:00',
            completed_at='2026-04-14T05:50:16+00:00',
            submitted_count=1,
            latest_operator_message='Submission finished for Acme / Backend Platform Engineer with status submitted.',
        )
    )

    payload = service.autonomous_status_payload()

    assert payload['latest_run']['run_id'] == 'auto-live'
    assert payload['latest_run']['run_type'] == 'submission'
    assert payload['latest_run']['status'] == 'completed'
    assert payload['latest_run']['submitted_count'] == 1


def test_run_autonomous_does_not_loop_on_blocked_application(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    ws.save_profile(profile)

    application = ws.find_application('001')
    assert application is not None
    ws.upsert_application(application.model_copy(update={'status': 'Ready to Submit'}))

    call_counter = {'discovery': 0}

    def fake_discovery_scan(self, *, run_id, run_type, limit=50):
        _ = self
        _ = run_id
        _ = run_type
        _ = limit
        call_counter['discovery'] += 1
        return {'targets': {'greenhouse': ['acme']}, 'discovered': 0, 'new_jobs': 0, 'updated_jobs': 0, 'duplicates': 0, 'saved_job_ids': []}

    async def fake_prepare(self, application_id, run_id):
        _ = self
        _ = run_id
        return SubmissionRecord(
            application_id=application_id,
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            event_status='needs_user_input',
            submit_ready=False,
        )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._run_discovery_scan', fake_discovery_scan)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)

    service.run_autonomous()

    assert call_counter['discovery'] == 0


def test_run_autonomous_records_failed_application_once_and_continues(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.ready_to_apply_threshold = 1
    ws.save_profile(profile)

    primary = ws.find_application('001')
    assert primary is not None
    ws.upsert_application(primary.model_copy(update={'status': 'Ready to Submit'}))
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            event_status='ready_for_submit',
            submit_ready=True,
        )
    )

    second_job = InboxJob(
        job_id='job-200',
        company='Bravo',
        company_key='bravo',
        title='Platform Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        url='https://boards.greenhouse.io/bravo/jobs/200',
        apply_url='https://boards.greenhouse.io/bravo/jobs/200',
        description='Second ready application.',
        workflow_state='pdf_ready',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-200',
        duplicate_cluster_key='job-200',
        screening=ScreeningDecision(approved=True, reasons=['fit'], confidence=0.9),
    )
    ws.save_job(second_job)
    ws.upsert_inbox_jobs([second_job])
    ws.upsert_application(
        ApplicationEntry(
            id='002',
            job_id='job-200',
            date='2026-04-06',
            company='Bravo',
            role='Platform Engineer',
            score=4.4,
            grade='A',
            status='Ready to Submit',
            pdf=True,
            report='reports/002-bravo-2026-04-06.md',
            url=second_job.url,
            source='greenhouse',
        )
    )
    ws.save_submission(
        SubmissionRecord(
            application_id='002',
            job_id='job-200',
            company='Bravo',
            role='Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            event_status='ready_for_submit',
            submit_ready=True,
        )
    )

    attempts: list[str] = []

    def fake_continue_after_ready(self, application_id, run_id):
        _ = (self, run_id)
        attempts.append(application_id)
        if application_id == '001':
            raise RuntimeError('lever-style submit failure')
        record = ws.load_submission(application_id)
        assert record is not None
        updated = record.model_copy(update={'status': 'submitted', 'submit_ready': False, 'submitted_at': '2026-04-06T00:00:00+00:00'})
        ws.save_submission(updated)
        application = ws.find_application(application_id)
        assert application is not None
        ws.upsert_application(application.model_copy(update={'status': 'Applied'}))
        return updated

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fake_continue_after_ready)
    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._run_discovery_scan',
        lambda self, *, run_id, run_type, limit=50: {
            'new_jobs': 0,
            'updated_jobs': 0,
            'saved_job_ids': [],
            'eligible_job_ids': [],
        },
    )

    result = service.run_autonomous(run_id='auto-failure-once')

    assert attempts == ['001', '002']
    assert result['failed_application_ids'] == ['001']
    assert result['submitted_application_ids'] == ['002']
    runs = ws.load_runs()
    assert runs[0].failed_application_ids == ['001']
    assert runs[0].submitted_application_ids == ['002']


def test_run_payloads_are_summarized_for_ui_clients(tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    ws.save_run(
        RunRecord(
            run_id='run-001',
            run_type='discover',
            status='completed',
            event_status='completed',
            processed_job_ids=['job-100', 'job-101'],
            evaluated_application_ids=['001'],
            submitted_application_ids=['001'],
            failed_application_ids=[],
            notes=['scan:new=2'],
            metrics={
                'scan': {
                    'eligible_job_ids': ['job-100', 'job-101'],
                    'duplicates': 1,
                },
                'submit_mode': 'auto_submit',
            },
        )
    )

    runs_payload = service.runs_history_payload(limit=5)
    latest_run = service.autonomous_status_payload()['latest_run']

    assert runs_payload['items'][0]['processed_count'] == 2
    assert runs_payload['items'][0]['evaluated_count'] == 1
    assert runs_payload['items'][0]['submitted_count'] == 1
    assert 'processed_job_ids' not in runs_payload['items'][0]
    assert runs_payload['items'][0]['metrics']['scan']['eligible_job_ids_count'] == 2
    assert latest_run['processed_count'] == 2
    assert latest_run['submitted_count'] == 1



def test_submit_application_reports_runtime_blocker_without_calling_adapter(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    application = ws.find_application('001')
    assert application is not None
    ws.upsert_application(application.model_copy(update={'status': 'Ready to Submit'}))
    ws.save_submission(
        SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            event_status='ready_for_submit',
            submit_ready=True,
            artifacts={'resume_pdf': 'output/cv-001-acme-2026-04-05.pdf'},
            plan={'source_kind': 'greenhouse', 'application_url': 'https://boards.greenhouse.io/acme/jobs/100'},
        )
    )

    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker',
        lambda self: {'key': 'runtime.playwright', 'message': 'Playwright browsers are missing.', 'inspection': {'browser_ok': False}},
    )

    record = anyio.run(service._submit_application_async, '001', 'run-blocked')
    events = ws.load_live_events(limit=10)

    assert record.status == 'blocked'
    assert record.event_status == 'runtime_blocked'
    assert record.last_error == 'Playwright browsers are missing.'
    assert any(event.event_type == 'submission.submit.blocked' for event in events)


def test_submit_application_records_uncertain_lever_captcha_result(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    lever_job = InboxJob(
        job_id='job-lever-1',
        company='Plaid',
        company_key='plaid',
        title='Technical Support Engineer',
        source='lever',
        source_kind='lever',
        source_job_id='lever-1',
        url='https://jobs.lever.co/plaid/lever-1',
        apply_url='https://jobs.lever.co/plaid/lever-1/apply',
        description='Lever application flow.',
        workflow_state='pdf_ready',
        board_family='lever',
        automation_tier='auto_submit_supported',
        job_identity_key='job-lever-1',
        duplicate_cluster_key='job-lever-1',
        screening=ScreeningDecision(approved=True, reasons=['fit'], confidence=0.9),
    )
    ws.save_job(lever_job)
    ws.upsert_inbox_jobs([lever_job])
    ws.upsert_application(
        ApplicationEntry(
            id='lever-001',
            job_id='job-lever-1',
            date='2026-04-06',
            company='Plaid',
            role='Technical Support Engineer',
            score=4.2,
            grade='B',
            status='Ready to Submit',
            pdf=True,
            report='reports/lever-001-plaid-2026-04-06.md',
            url=lever_job.url,
            source='lever',
        )
    )
    ws.save_submission(
        SubmissionRecord(
            application_id='lever-001',
            job_id='job-lever-1',
            company='Plaid',
            role='Technical Support Engineer',
            source='lever',
            status='ready_for_submit',
            event_status='ready_for_submit',
            submit_ready=True,
            plan={'source_kind': 'lever', 'application_url': lever_job.apply_url},
        )
    )

    class _LeverAdapter:
        async def submit(self, posting, plan, output_dir):
            _ = (posting, output_dir)
            return SubmissionResult(
                status=JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                submitted=False,
                uncertain=True,
                message='Apply flow is blocked by captcha or anti-bot controls',
                plan=plan,
                evidence=SubmissionEvidence(failure_reason='captcha_detected'),
            )

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._adapter_for_job', lambda self, job: _LeverAdapter())
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)

    record = anyio.run(service._submit_application_async, 'lever-001', 'run-lever-1')
    application = ws.find_application('lever-001')
    events = ws.load_live_events(limit=10)

    assert record.status == 'submission_uncertain'
    assert record.submit_ready is False
    assert record.result['evidence']['failure_reason'] == 'captcha_detected'
    assert application is not None
    assert application.status == 'Submission Uncertain'
    assert any(event.event_type == 'submission.submit.completed' and event.status == 'warning' for event in events)


def test_run_autonomous_avoids_no_eligible_note_after_discovery_error(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    application = ws.find_application('001')
    assert application is not None
    ws.upsert_application(application.model_copy(update={'status': 'Applied'}))

    def fake_discovery_error(self, *, run_id, run_type, limit=50):
        _ = (self, run_id, run_type, limit)
        raise RuntimeError("lmstudio-draft-cover-letter-writer: provider error payload")

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._run_discovery_scan', fake_discovery_error)

    result = service.run_autonomous()

    notes = list(result.get('notes') or [])
    assert any("Discovery error:" in note for note in notes)
    assert all("No eligible applications are ready for continuation." not in note for note in notes)


def test_run_autonomous_finishes_with_manual_answers_pending(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.ready_to_apply_threshold = 1
    ws.save_profile(profile)

    application = ws.find_application('001')
    assert application is not None
    ws.upsert_application(application.model_copy(update={'status': 'PDF Ready'}))

    async def fake_prepare(self, application_id, run_id):
        _ = (self, application_id, run_id)
        record = SubmissionRecord(
            application_id='001',
            job_id='job-100',
            company='Acme',
            role='Backend Platform Engineer',
            source='greenhouse',
            status='needs_user_input',
            event_status='needs_user_input',
            submit_ready=False,
            questions=[
                SubmissionQuestion(
                    question_id='work-auth',
                    prompt_text='Are you legally authorized to work in the United States?',
                    normalized_key='work-auth',
                    question_type='boolean',
                    widget_type='radio_group',
                    required=True,
                    needs_user_input=True,
                )
            ],
            missing_required_fields=['Are you legally authorized to work in the United States?'],
            ungrounded_answers=['Are you legally authorized to work in the United States?'],
        )
        ws.save_submission(record)
        return record

    async def fake_manual_handoff(self, application_id, run_id):
        _ = (self, application_id, run_id)
        return True

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._open_manual_handoff_preview_async', fake_manual_handoff)
    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._autonomous_discovery_step',
        lambda self, run_id, notes: {'stop': True, 'terminal_error': None, 'new_jobs': 0, 'discovery_exhausted': True},
    )

    result = service.run_autonomous(run_id='auto-blocked')
    live_state = ws.load_live_state()
    runs = ws.load_runs()

    assert result['paused_for_questions'] is True
    assert live_state.status == 'completed_with_failures'
    assert live_state.stage == 'question_resolution'
    assert runs[0].status == 'completed_with_failures'


def test_run_autonomous_continues_after_manual_blocker(monkeypatch, tmp_path: Path) -> None:
    service, ws = _seed_service_workspace(tmp_path)
    profile = ws.load_profile()
    profile.runtime.automation.enabled = True
    profile.runtime.automation.submit_enabled = True
    profile.runtime.automation.ready_to_apply_threshold = 1
    ws.save_profile(profile)

    second_job = InboxJob(
        job_id='job-200',
        company='Bravo',
        company_key='bravo',
        title='Platform Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='200',
        url='https://boards.greenhouse.io/bravo/jobs/200',
        apply_url='https://boards.greenhouse.io/bravo/jobs/200',
        location='Remote',
        description='Build platform systems.',
        workflow_state='pdf_ready',
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key='job-200',
        duplicate_cluster_key='bravo-platform-engineer',
    )
    ws.save_job(second_job)
    ws.upsert_inbox_jobs([second_job])
    second_report_path = ws.report_path_for('002', 'Bravo', '2026-04-05')
    second_report_path.write_text('# Bravo Evaluation\n', encoding='utf-8')
    ws.resume_pdf_path_for('002', 'Bravo', '2026-04-05').write_bytes(b'%PDF-1.4\n%stub\n')
    ws.upsert_application(
        ApplicationEntry(
            id='002',
            job_id='job-200',
            date='2026-04-05',
            company='Bravo',
            role='Platform Engineer',
            score=4.4,
            grade='A',
            status='PDF Ready',
            pdf=True,
            report=ws.relative_path(second_report_path),
            url=second_job.url,
            source='greenhouse',
        )
    )

    async def fake_prepare(self, application_id, run_id):
        _ = (self, run_id)
        if application_id == '001':
            record = SubmissionRecord(
                application_id='001',
                job_id='job-100',
                company='Acme',
                role='Backend Platform Engineer',
                source='greenhouse',
                status='needs_user_input',
                event_status='needs_user_input',
                submit_ready=False,
                questions=[
                    SubmissionQuestion(
                        question_id='english',
                        prompt_text='Are you fluent in English?',
                        normalized_key='are-you-fluent-in-english',
                        question_type='boolean',
                        widget_type='radio_group',
                        required=True,
                        needs_user_input=True,
                    )
                ],
                missing_required_fields=['Are you fluent in English?'],
                ungrounded_answers=['Are you fluent in English?'],
            )
            ws.save_submission(record)
            return record
        record = SubmissionRecord(
            application_id='002',
            job_id='job-200',
            company='Bravo',
            role='Platform Engineer',
            source='greenhouse',
            status='ready_for_submit',
            event_status='ready_for_submit',
            submit_ready=True,
        )
        ws.save_submission(record)
        return record

    def fake_continue(self, application_id, run_id):
        _ = (self, run_id)
        assert application_id == '002'
        record = SubmissionRecord(
            application_id='002',
            job_id='job-200',
            company='Bravo',
            role='Platform Engineer',
            source='greenhouse',
            status='submitted',
            event_status='submitted',
            submit_ready=False,
        )
        ws.save_submission(record)
        application = ws.find_application('002')
        assert application is not None
        ws.upsert_application(application.model_copy(update={'status': 'Applied'}))
        return record

    async def fake_manual_handoff(self, application_id, run_id):
        _ = (self, run_id)
        return application_id == '001'

    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._prepare_submission_async', fake_prepare)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._continue_after_ready', fake_continue)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._open_manual_handoff_preview_async', fake_manual_handoff)
    monkeypatch.setattr('findmyjob.filefirst.service.FileFirstOperatorService._browser_runtime_blocker', lambda self: None)
    monkeypatch.setattr(
        'findmyjob.filefirst.service.FileFirstOperatorService._autonomous_discovery_step',
        lambda self, run_id, notes: {'stop': True, 'terminal_error': None, 'new_jobs': 0, 'discovery_exhausted': True},
    )

    result = service.run_autonomous(run_id='auto-continue')
    live_state = ws.load_live_state()

    assert result['paused_for_questions'] is True
    assert result['submitted_application_ids'] == ['002']
    assert ws.load_submission('001').status == 'needs_user_input'
    assert ws.load_submission('002').status == 'submitted'
    assert live_state.status == 'completed_with_failures'
