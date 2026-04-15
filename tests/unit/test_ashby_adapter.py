from __future__ import annotations

import anyio
import httpx
from pathlib import Path

from findmyjob.core.enums import JobLifecycleStatus
from findmyjob.core.enums import QuestionType, VerificationStatus
from findmyjob.core.types import ApplicationQuestion, GroundedAnswer, SubmissionPlan, SubmissionResult
from findmyjob.sources.adapters.ashby import AshbyAdapter
from findmyjob.sources.contracts import DiscoveryQuery
from findmyjob.sources.normalizer import build_normalized_job


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _TextResponse:
    def __init__(self, text: str):
        self.text = text


def test_ashby_discover_populates_compensation(monkeypatch) -> None:
    async def fake_post(client, url, **kwargs):
        _ = client, url
        payload = kwargs['json']
        if payload['operationName'] == 'ApiJobBoardWithTeams':
            return _Response(
                {
                    'data': {
                        'jobBoard': {
                            'teams': [{'id': 'team-platform', 'name': 'Platform', 'externalName': None, 'parentTeamId': None}],
                            'jobPostings': [
                                {
                                    'id': 'ashby-1',
                                    'title': 'Backend Engineer',
                                    'employmentType': 'FullTime',
                                    'locationName': 'Remote - United States',
                                    'locationAddress': None,
                                    'workplaceType': 'Remote',
                                    'compensationTierSummary': '$81K - $87K',
                                    'teamId': 'team-platform',
                                }
                            ],
                        }
                    }
                }
            )
        return _Response(
            {
                'data': {
                    'jobPosting': {
                        'id': 'ashby-1',
                        'title': 'Backend Engineer',
                        'departmentName': 'Engineering',
                        'departmentExternalName': 'Engineering',
                        'teamNames': ['Platform'],
                        'locationName': 'Remote - United States',
                        'locationAddress': None,
                        'workplaceType': 'Remote',
                        'employmentType': 'FullTime',
                        'descriptionHtml': '<p>Build backend systems.</p>',
                        'publishedDate': '2026-04-10',
                        'secondaryLocationNames': [],
                        'compensationTierSummary': '$81K - $87K',
                        'compensationTiers': [],
                        'scrapeableCompensationSalarySummary': '$81K - $87K',
                        'linkedData': {},
                    }
                }
            }
        )

    monkeypatch.setattr('findmyjob.sources.adapters.ashby.http_post_with_retry', fake_post)
    adapter = AshbyAdapter(['acme'])

    async def run():
        async with httpx.AsyncClient() as client:
            return await adapter.discover(client, DiscoveryQuery())

    jobs = anyio.run(run)

    assert len(jobs) == 1
    posting, raw = jobs[0]
    assert raw['teamName'] == 'Platform'
    assert raw['compensation'][0]['min'] == 81000
    assert posting.compensation_min == 81000
    assert posting.compensation_max == 87000
    assert posting.compensation_currency == 'USD'
    assert posting.description == 'Build backend systems.'
    assert posting.posting_url == 'https://jobs.ashbyhq.com/acme/ashby-1'
    assert posting.apply_url == 'https://jobs.ashbyhq.com/acme/ashby-1/application'


def test_ashby_discover_keeps_brief_posting_when_detail_fetch_fails(monkeypatch) -> None:
    async def fake_post(client, url, **kwargs):
        _ = client, url
        payload = kwargs['json']
        if payload['operationName'] == 'ApiJobBoardWithTeams':
            return _Response(
                {
                    'data': {
                        'jobBoard': {
                            'teams': [{'id': 'team-platform', 'name': 'Platform', 'externalName': None, 'parentTeamId': None}],
                            'jobPostings': [
                                {
                                    'id': 'ashby-1',
                                    'title': 'Backend Engineer',
                                    'employmentType': 'FullTime',
                                    'locationName': 'Remote - United States',
                                    'locationAddress': None,
                                    'workplaceType': 'Remote',
                                    'compensationTierSummary': '$81K - $87K',
                                    'teamId': 'team-platform',
                                }
                            ],
                        }
                    }
                }
            )
        return _Response({'errors': [{'message': 'schema drift'}]})

    monkeypatch.setattr('findmyjob.sources.adapters.ashby.http_post_with_retry', fake_post)
    adapter = AshbyAdapter(['acme'])

    async def run():
        async with httpx.AsyncClient() as client:
            return await adapter.discover(client, DiscoveryQuery())

    jobs = anyio.run(run)

    assert len(jobs) == 1
    posting, raw = jobs[0]
    assert posting.description == ''
    assert posting.compensation_min == 81000
    assert 'detail_fetch_error' in raw
    assert 'schema drift' in raw['detail_fetch_error']
    assert posting.apply_url == 'https://jobs.ashbyhq.com/acme/ashby-1/application'


def test_ashby_load_application_contract_merges_rendered_dom_fields(monkeypatch) -> None:
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='ashby',
        source_kind='ashby',
        source_job_id='ashby-1',
        posting_url='https://jobs.ashbyhq.com/acme/ashby-1',
        apply_url='https://jobs.ashbyhq.com/acme/ashby-1/application',
        location_raw='Remote - United States',
        employment_type='FullTime',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )

    async def fake_get(client, url):
        _ = client, url
        return _TextResponse("<html><form><input name='email' type='email'></form></html>")

    class _Submitter:
        def __init__(self, **kwargs):
            _ = kwargs

        async def inspect_form(self, url: str):
            _ = url
            return [
                {
                    'tag': 'input',
                    'type': 'text',
                    'field_type': 'text',
                    'widget_type': 'text',
                    'name': '_systemfield_name',
                    'id': '_systemfield_name',
                    'required': True,
                    'label': 'Full Name',
                    'source_snapshot_ref': 'input:_systemfield_name',
                },
                {
                    'tag': 'input',
                    'type': 'radio',
                    'field_type': 'radio',
                    'widget_type': 'radio_group',
                    'name': 'eeoc_gender',
                    'id': 'eeoc_gender_1',
                    'required': False,
                    'label': 'Gender',
                    'group_label': 'Gender',
                    'option_label': 'Male',
                    'option_value': 'Male',
                    'source_snapshot_ref': 'radio:eeoc_gender',
                },
                {
                    'tag': 'input',
                    'type': 'radio',
                    'field_type': 'radio',
                    'widget_type': 'radio_group',
                    'name': 'eeoc_gender',
                    'id': 'eeoc_gender_2',
                    'required': False,
                    'label': 'Gender',
                    'group_label': 'Gender',
                    'option_label': 'Female',
                    'option_value': 'Female',
                    'source_snapshot_ref': 'radio:eeoc_gender',
                },
                {
                    'tag': 'textarea',
                    'type': '',
                    'field_type': 'textarea',
                    'widget_type': 'textarea',
                    'name': 'g-recaptcha-response',
                    'id': 'g-recaptcha-response',
                    'required': False,
                    'label': 'g-recaptcha-response',
                    'source_snapshot_ref': 'textarea:g-recaptcha-response',
                },
            ]

    monkeypatch.setattr('findmyjob.sources.adapters.ashby.http_get_with_retry', fake_get)
    monkeypatch.setattr('findmyjob.sources.adapters.ashby.PlaywrightSubmitter', _Submitter)
    adapter = AshbyAdapter(['acme'])

    async def run():
        async with httpx.AsyncClient() as client:
            return await adapter.load_application_contract(client, posting)

    result = anyio.run(run)

    prompts = [question.prompt_text for question in result.questions]
    assert 'Full Name' in prompts
    assert 'Gender' in prompts
    assert 'g-recaptcha-response' not in prompts


def test_ashby_bind_answers_uses_canonical_application_url() -> None:
    adapter = AshbyAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='ashby',
        source_kind='ashby',
        source_job_id='ashby-1',
        posting_url='https://jobs.ashbyhq.com/acme/ashby-1',
        apply_url='https://jobs.ashbyhq.com/acme/ashby-1',
        location_raw='Remote - United States',
        employment_type='FullTime',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )

    plan = adapter.bind_answers(posting, [], {})

    assert plan.application_url == 'https://jobs.ashbyhq.com/acme/ashby-1/application'


def test_ashby_adapter_preview_submission_uses_canonical_application_url(monkeypatch) -> None:
    adapter = AshbyAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='ashby',
        source_kind='ashby',
        source_job_id='ashby-1',
        posting_url='https://jobs.ashbyhq.com/acme/ashby-1',
        apply_url='https://jobs.ashbyhq.com/acme/ashby-1',
        location_raw='Remote - United States',
        employment_type='FullTime',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )
    plan = SubmissionPlan(source_kind='ashby', application_url='https://example.com/stale', fields=[])
    called: list[str] = []

    class _Submitter:
        def __init__(self, **kwargs):
            _ = kwargs

        async def preview_generic_form(self, url: str, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
            _ = plan
            _ = output_dir
            called.append(url)
            return SubmissionResult(status=JobLifecycleStatus.READY_FOR_REVIEW, submitted=False, uncertain=False)

    monkeypatch.setattr('findmyjob.sources.adapters.ashby.PlaywrightSubmitter', _Submitter)

    result = anyio.run(adapter.preview_submission, posting, plan, Path('.'))

    assert result.status == JobLifecycleStatus.READY_FOR_REVIEW
    assert called == ['https://jobs.ashbyhq.com/acme/ashby-1/application']


def test_ashby_adapter_execute_submission_uses_canonical_application_url(monkeypatch) -> None:
    adapter = AshbyAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='ashby',
        source_kind='ashby',
        source_job_id='ashby-1',
        posting_url='https://jobs.ashbyhq.com/acme/ashby-1',
        apply_url='https://jobs.ashbyhq.com/acme/ashby-1',
        location_raw='Remote - United States',
        employment_type='FullTime',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )
    plan = SubmissionPlan(source_kind='ashby', application_url='https://example.com/stale', fields=[])
    called: list[str] = []

    class _Submitter:
        def __init__(self, **kwargs):
            _ = kwargs

        async def submit_generic_form(self, url: str, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
            _ = plan
            _ = output_dir
            called.append(url)
            return SubmissionResult(status=JobLifecycleStatus.SUBMITTED, submitted=True, uncertain=False)

    monkeypatch.setattr('findmyjob.sources.adapters.ashby.PlaywrightSubmitter', _Submitter)

    result = anyio.run(adapter.execute_submission, posting, plan, Path('.'))

    assert result.status == JobLifecycleStatus.SUBMITTED
    assert result.submitted is True
    assert called == ['https://jobs.ashbyhq.com/acme/ashby-1/application']


def test_ashby_bind_answers_uses_application_question_fields_without_field_config() -> None:
    adapter = AshbyAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='ashby',
        source_kind='ashby',
        source_job_id='ashby-1',
        posting_url='https://jobs.ashbyhq.com/acme/ashby-1',
        apply_url='https://jobs.ashbyhq.com/acme/ashby-1/application',
        location_raw='Remote - United States',
        employment_type='FullTime',
        compensation=None,
        description='Build backend systems.',
        notes={'board': 'acme'},
    )
    question = ApplicationQuestion(
        source_field_name='location',
        prompt_text='Current location',
        question_type=QuestionType.NARRATIVE,
        widget_type='text',
        required=True,
        input_role='data',
        section='profile',
        option_details=[{'label': 'Austin', 'value': 'Austin, TX'}],
        submission_binding={'field': 'candidate.location'},
        source_snapshot_ref='snapshot-1',
    )
    answer = GroundedAnswer(
        question='Current location',
        answer='Austin, TX',
        confidence=1.0,
        verification_status=VerificationStatus.VERIFIED,
    )

    plan = adapter.bind_answers(posting, [(question, answer)], {})

    assert plan.missing_required_fields == []
    assert len(plan.fields) == 1
    field = plan.fields[0]
    assert field.value == 'Austin, TX'
    assert field.metadata['section'] == 'profile'
    assert field.metadata['option_details'] == [{'label': 'Austin', 'value': 'Austin, TX'}]
    assert field.metadata['submission_binding'] == {'field': 'candidate.location'}
