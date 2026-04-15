from __future__ import annotations

from findmyjob.core.enums import QuestionType, VerificationStatus
from findmyjob.core.types import ApplicationQuestion, GroundedAnswer
from findmyjob.sources.adapters.lever import LeverAdapter
from findmyjob.sources.normalizer import build_normalized_job


def test_lever_bind_answers_uses_application_question_fields_without_field_config() -> None:
    adapter = LeverAdapter(['acme'])
    posting = build_normalized_job(
        company_name='Acme',
        title='Engineer',
        source='lever',
        source_kind='lever',
        source_job_id='lever-1',
        posting_url='https://jobs.lever.co/acme/lever-1',
        apply_url='https://jobs.lever.co/acme/lever-1',
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

    assert plan.application_url == 'https://jobs.lever.co/acme/lever-1'
    assert plan.missing_required_fields == []
    assert len(plan.fields) == 1
    field = plan.fields[0]
    assert field.value == 'Austin, TX'
    assert field.metadata['section'] == 'profile'
    assert field.metadata['option_details'] == [{'label': 'Austin', 'value': 'Austin, TX'}]
    assert field.metadata['submission_binding'] == {'field': 'candidate.location'}
