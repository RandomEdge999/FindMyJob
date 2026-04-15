from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio

from findmyjob.core.enums import JobLifecycleStatus, QuestionType
from findmyjob.core.types import ApplicationQuestion, SubmissionPlan, SubmissionResult
from findmyjob.sources.adapters.greenhouse import GreenhouseAdapter
from findmyjob.sources.contracts import DiscoveryQuery
from findmyjob.sources.normalizer import build_normalized_job


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url: str, params=None):
        _ = url
        _ = params
        return _Response(self._payload)


def test_greenhouse_adapter_canonicalizes_careerpuck_urls_in_discovery() -> None:
    adapter = GreenhouseAdapter(['acme'])
    payload = {
        'jobs': [
            {
                'id': 123,
                'absolute_url': 'https://app.careerpuck.com/job-board/acme/job/123?gh_jid=123',
                'title': 'Engineer',
                'content': 'Build backend systems.',
                'offices': [],
                'metadata': {},
            }
        ]
    }

    results = anyio.run(adapter.discover, _Client(payload), DiscoveryQuery())

    posting, _raw = results[0]
    assert posting.posting_url == 'https://job-boards.greenhouse.io/acme/jobs/123'
    assert posting.apply_url == 'https://job-boards.greenhouse.io/acme/jobs/123#application'


def test_greenhouse_adapter_preview_submission_uses_canonical_application_url(monkeypatch) -> None:
    adapter = GreenhouseAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='123',
        posting_url='https://app.careerpuck.com/job-board/acme/job/123?gh_jid=123',
        apply_url='https://app.careerpuck.com/job-board/acme/job/123?gh_jid=123',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )
    plan = SubmissionPlan(source_kind='greenhouse', application_url='https://example.com/stale', fields=[])
    called: list[str] = []

    class _Submitter:
        async def preview_greenhouse(
            self,
            url: str,
            plan: SubmissionPlan,
            output_dir: Path,
            *,
            keep_browser_open: bool = False,
        ) -> SubmissionResult:
            _ = plan
            _ = output_dir
            _ = keep_browser_open
            called.append(url)
            return SubmissionResult(status=JobLifecycleStatus.READY_FOR_REVIEW, submitted=False, uncertain=False)

    monkeypatch.setattr(adapter, '_submitter_for_job', lambda job: _Submitter())

    result = anyio.run(adapter.preview_submission, posting, plan, Path('.'))

    assert result.status == JobLifecycleStatus.READY_FOR_REVIEW
    assert called == ['https://job-boards.greenhouse.io/acme/jobs/123#application']


def test_greenhouse_adapter_uses_cover_letter_artifact_for_ambiguous_attach_prompt() -> None:
    adapter = GreenhouseAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='123',
        posting_url='https://job-boards.greenhouse.io/acme/jobs/123',
        apply_url='https://job-boards.greenhouse.io/acme/jobs/123#application',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )
    question = ApplicationQuestion(
        source_field_name='cover_letter',
        prompt_text='Attach',
        normalized_key='attach',
        question_type=QuestionType.FILE,
        widget_type='file',
        submission_binding={'name': 'cover_letter', 'id': 'cover_letter', 'tag': 'input', 'type': 'file'},
    )
    answer = SimpleNamespace(candidate_answer=None, needs_user_input=False)

    plan = adapter.bind_answers(
        posting,
        [(question, answer)],
        {
            'resume_pdf': 'output/resume.pdf',
            'cover_letter_pdf': 'output/cover_letter.pdf',
        },
    )

    assert len(plan.fields) == 1
    assert plan.fields[0].artifact_binding is not None
    assert str(plan.fields[0].artifact_binding.path) == 'output/cover_letter.pdf'


def test_greenhouse_adapter_load_application_contract_preserves_standard_fields_without_dom_inspection(monkeypatch) -> None:
    adapter = GreenhouseAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='123',
        posting_url='https://job-boards.greenhouse.io/acme/jobs/123',
        apply_url='https://job-boards.greenhouse.io/acme/jobs/123#application',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )
    payload = {
        'questions': [
            {
                'label': 'Do you hold an AWS certification?',
                'required': True,
                'fields': [{'name': 'aws_certification', 'type': 'multi_value_single_select'}],
                'answer_options': ['Yes', 'No'],
            }
        ]
    }

    class _Submitter:
        async def inspect_form(self, url: str):
            _ = url
            raise RuntimeError('rendered form unavailable')

    monkeypatch.setattr(adapter, '_submitter_for_job', lambda job: _Submitter())

    result = anyio.run(adapter.load_application_contract, _Client(payload), posting)
    names = {question.source_field_name for question in result.questions}

    assert 'aws_certification' in names
    assert 'first_name' in names
    assert 'last_name' in names
    assert 'email' in names
    assert 'resume' in names
