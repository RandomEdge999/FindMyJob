import pytest

from findmyjob.core.enums import FactKind, ModelRole, QuestionType, Sensitivity, VerificationStatus
from findmyjob.core.types import ProfileFact
from findmyjob.grounding.service import GroundingService


@pytest.mark.anyio
async def test_grounding_uses_answer_memory() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id="contact-1",
            kind=FactKind.CONTACT,
            payload={"email": "user@example.com"},
            sensitivity=Sensitivity.LOW,
        )
    ]
    answer = await service.answer_question(
        "What is your email address?",
        facts,
        normalized_key="email-address",
        answer_memory=[{"canonical_question": "email-address", "answer_text": "cached@example.com", "grounded_fact_ids": ["contact-1"], "approved": True}],
    )
    assert answer.answer == "cached@example.com"
    assert answer.provenance == "answer_memory"
    assert answer.verification_status == VerificationStatus.VERIFIED


@pytest.mark.anyio
async def test_grounding_uses_answer_memory_with_context_constraints() -> None:
    service = GroundingService()
    answer = await service.answer_question(
        "What gender identity do you most closely identify with?",
        [],
        normalized_key="what-gender-identity-do-you-most-closely-identify-with",
        answer_memory=[
            {
                "canonical_question": "what-gender-identity-do-you-most-closely-identify-with",
                "answer_text": "Prefer not to disclose",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {
                    "question_type": "unknown",
                    "source_adapter": "greenhouse",
                    "option_signature": "",
                },
            }
        ],
        memory_context={
            "question_type": "unknown",
            "source_adapter": "greenhouse",
            "option_signature": "",
        },
    )

    assert answer.answer == "Prefer not to disclose"
    assert answer.reason == "answer_memory_hit"


@pytest.mark.anyio
async def test_grounding_treats_missing_constraint_keys_as_wildcards() -> None:
    service = GroundingService()
    answer = await service.answer_question(
        "What gender identity do you most closely identify with?",
        [],
        normalized_key="what-gender-identity-do-you-most-closely-identify-with",
        answer_memory=[
            {
                "canonical_question": "what-gender-identity-do-you-most-closely-identify-with",
                "answer_text": "Prefer not to disclose",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {},
            }
        ],
        memory_context={
            "question_type": "select",
            "source_adapter": "greenhouse",
            "option_signature": "female|male|i don't wish to answer",
        },
    )

    assert answer.answer == "Prefer not to disclose"
    assert answer.reason == "answer_memory_hit"


@pytest.mark.anyio
async def test_grounding_prefers_more_specific_answer_memory_match() -> None:
    service = GroundingService()
    answer = await service.answer_question(
        "Are you a veteran/have you served in the military?",
        [],
        normalized_key="are-you-a-veteran-have-you-served-in-the-military",
        answer_memory=[
            {
                "canonical_question": "are-you-a-veteran-have-you-served-in-the-military",
                "answer_text": "I am not a protected veteran",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {},
            },
            {
                "canonical_question": "are-you-a-veteran-have-you-served-in-the-military",
                "answer_text": "No military service",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {
                    "question_type": "sensitive",
                    "source_adapter": "greenhouse",
                    "option_signature": "active duty|military spouse|no military service",
                },
            },
        ],
        memory_context={
            "question_type": "sensitive",
            "source_adapter": "greenhouse",
            "option_signature": "active duty|military spouse|no military service",
        },
    )

    assert answer.answer == "No military service"
    assert answer.reason == "answer_memory_hit"


@pytest.mark.anyio
async def test_grounding_prefers_option_matching_answer_memory_variant() -> None:
    service = GroundingService()
    answer = await service.answer_question(
        "Are you a veteran/have you served in the military?",
        [],
        options=["Active duty", "Military spouse", "No military service"],
        normalized_key="are-you-a-veteran-have-you-served-in-the-military",
        answer_memory=[
            {
                "canonical_question": "are-you-a-veteran-have-you-served-in-the-military",
                "answer_text": "I am not a protected veteran",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {},
            },
            {
                "canonical_question": "are-you-a-veteran-have-you-served-in-the-military",
                "answer_text": "No military service",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {},
            },
        ],
        memory_context={
            "question_type": "sensitive",
            "source_adapter": "greenhouse",
            "option_signature": "active duty|military spouse|no military service",
        },
    )

    assert answer.answer == "No military service"
    assert answer.reason == "answer_memory_hit"


@pytest.mark.anyio
async def test_grounding_ignores_nonmatching_memory_when_fact_can_match_live_option() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id="demo-1",
            kind=FactKind.PERSONAL,
            payload={
                "category": "demographic",
                "label": "sexual_orientation",
                "value": "Straight",
            },
            sensitivity=Sensitivity.HIGH,
            allowed_for_generation=True,
        )
    ]
    answer = await service.answer_question(
        "What sexual orientation do you most closely identify with?",
        facts,
        options=["Bisexual", "Gay or Lesbian", "Straight", "Choose not to disclose"],
        normalized_key="what-sexual-orientation-do-you-most-closely-identify-with",
        answer_memory=[
            {
                "canonical_question": "what-sexual-orientation-do-you-most-closely-identify-with",
                "answer_text": "Self-described",
                "grounded_fact_ids": [],
                "approved": True,
                "context_constraints": {},
            }
        ],
        memory_context={
            "question_type": "sensitive",
            "source_adapter": "greenhouse",
            "option_signature": "bisexual|choose not to disclose|gay or lesbian|straight",
        },
    )

    assert answer.answer == "Straight"
    assert answer.reason == "demographic_sexual_orientation_rule"


@pytest.mark.anyio
async def test_grounding_blocks_why_us_questions() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id="work-1",
            kind=FactKind.WORK,
            payload={"summary": "Built reliable APIs."},
            sensitivity=Sensitivity.LOW,
        )
    ]
    answer = await service.answer_question("Why do you want to work here?", facts)
    assert answer.question_type == QuestionType.NARRATIVE
    assert answer.verification_status == VerificationStatus.NEEDS_USER_INPUT
    assert answer.answer is None



@pytest.mark.anyio
async def test_grounding_narrative_uses_relevant_facts_for_model_answer() -> None:
    class FakeRouter:
        async def generate_text_with_profile(self, role, prompt, system_prompt=None):
            assert role == ModelRole.QUESTION_ANSWERER
            first_fact_line = next(line for line in prompt.splitlines() if line.startswith('- '))
            assert 'transformer-based sentiment and emotion signals' in first_fact_line.lower()
            return 'I built transformer-based sentiment and emotion signals over time-stamped posts.', 'qa-profile'

        async def generate_json_with_profile(self, role, prompt, system_prompt=None):
            assert role == ModelRole.VERIFIER
            return {'supported': True, 'unsupported_segments': []}, 'verifier-profile'

    service = GroundingService(router=FakeRouter())
    facts = [
        ProfileFact(
            fact_id='work-1',
            kind=FactKind.WORK,
            payload={'summary': 'Built reliable APIs for internal tools.'},
            sensitivity=Sensitivity.LOW,
        ),
        ProfileFact(
            fact_id='project-1',
            kind=FactKind.PROJECT,
            payload={'summary': 'Built transformer-based sentiment and emotion signals over time-stamped posts.'},
            sensitivity=Sensitivity.LOW,
        ),
    ]

    answer = await service.answer_question(
        'Describe your experience building transformer-based sentiment and emotion signals over time-stamped posts.',
        facts,
    )

    assert answer.provenance == 'model:qa-profile'
    assert answer.used_fact_ids[0] == 'project-1'
    assert answer.verification_status == VerificationStatus.REVIEW_REQUIRED
    assert 'transformer-based sentiment and emotion signals' in (answer.answer or '').lower()


@pytest.mark.anyio
async def test_grounding_answers_citizenship_select_from_country_code() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='auth-1',
            kind=FactKind.AUTHORIZATION,
            payload={'country_code': 'US'},
            sensitivity=Sensitivity.LOW,
        )
    ]

    answer = await service.answer_question(
        'Which country/region do you have citizenship in?',
        facts,
        options=['Canada', 'United States', 'Mexico'],
    )

    assert answer.answer == 'United States'
    assert answer.selected_option_values == ['United States']


@pytest.mark.anyio
async def test_grounding_answers_state_select_from_region_code() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='loc-state',
            kind=FactKind.LOCATION,
            payload={'region_code': 'TN'},
            sensitivity=Sensitivity.LOW,
        )
    ]

    answer = await service.answer_question(
        'Which U.S. State or Canadian Province do you reside in?',
        facts,
        options=['Alabama', 'Tennessee', 'Texas'],
    )

    assert answer.answer == 'Tennessee'
    assert answer.selected_option_values == ['Tennessee']


@pytest.mark.anyio
async def test_grounding_answers_work_without_sponsorship_boolean() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='auth-2',
            kind=FactKind.AUTHORIZATION,
            payload={'is_authorized': True, 'requires_future_sponsorship': False},
            sensitivity=Sensitivity.MEDIUM,
        )
    ]

    answer = await service.answer_question('Can you legally work in the United States without sponsorship?', facts)

    assert answer.answer == 'Yes'
    assert answer.confidence == 1.0
    assert answer.verification_status == VerificationStatus.REVIEW_REQUIRED


@pytest.mark.anyio
async def test_grounding_answers_english_fluency_from_language_fact() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='skill-language-english',
            kind=FactKind.SKILL,
            payload={'name': 'English', 'category': 'language', 'summary': 'English'},
            sensitivity=Sensitivity.LOW,
        )
    ]

    answer = await service.answer_question('Are you fluent in English?', facts)

    assert answer.answer == 'Yes'
    assert answer.reason == 'language_rule'
    assert answer.verification_status == VerificationStatus.REVIEW_REQUIRED


@pytest.mark.anyio
async def test_grounding_uses_model_fallback_for_safe_boolean_questions() -> None:
    class FakeRouter:
        async def generate_json_with_profile(self, role, prompt, system_prompt=None):
            assert role == ModelRole.QUESTION_ANSWERER
            assert 'Question: Do you communicate clearly with distributed teams?' in prompt
            return {
                'answer': 'Yes',
                'confidence': 0.82,
                'reason': 'Strong written and collaborative experience in the provided facts.',
                'uncertain': False,
            }, 'qa-bool'

    service = GroundingService(router=FakeRouter())
    facts = [
        ProfileFact(
            fact_id='work-english-1',
            kind=FactKind.WORK,
            payload={'summary': 'Worked closely with distributed teams and communicated technical findings clearly.'},
            sensitivity=Sensitivity.LOW,
        )
    ]

    answer = await service.answer_question('Do you communicate clearly with distributed teams?', facts)

    assert answer.answer == 'Yes'
    assert answer.provenance == 'model:qa-bool'
    assert answer.reason.startswith('model_boolean:')
    assert answer.verification_status == VerificationStatus.REVIEW_REQUIRED


@pytest.mark.anyio
async def test_grounding_uses_privacy_fallback_for_required_pronouns() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='contact-1',
            kind=FactKind.CONTACT,
            payload={'name': 'Test User'},
            sensitivity=Sensitivity.LOW,
        )
    ]

    answer = await service.answer_question(
        'Pronouns',
        facts,
        options=['He/him/his', 'She/her/hers', 'They/them/theirs', 'I prefer not to say'],
    )

    assert answer.answer == 'I prefer not to say'
    assert answer.reason == 'privacy_fallback'
    assert answer.verification_status == VerificationStatus.REVIEW_REQUIRED


@pytest.mark.anyio
async def test_grounding_skips_privacy_fallback_for_optional_pronouns() -> None:
    service = GroundingService()

    answer = await service.answer_question(
        'Pronouns',
        [],
        options=['He/him/his', 'She/her/hers', 'They/them/theirs', 'I prefer not to say'],
        allow_sensitive_fallback=False,
    )

    assert answer.answer is None
    assert answer.reason != 'privacy_fallback'


@pytest.mark.anyio
async def test_grounding_uses_default_decline_for_required_sensitive_fields_without_options() -> None:
    service = GroundingService()

    gender_answer = await service.answer_question(
        'Gender*',
        [],
        allow_sensitive_fallback=True,
    )
    disability_answer = await service.answer_question(
        'Disability Status*',
        [],
        allow_sensitive_fallback=True,
    )

    assert gender_answer.answer == 'Decline To Self Identify'
    assert disability_answer.answer == 'I do not want to answer'


@pytest.mark.anyio
async def test_grounding_uses_operator_demographic_facts_for_sensitive_selects() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='demo-gender',
            kind=FactKind.PERSONAL,
            payload={'category': 'demographic', 'label': 'gender', 'value': 'Male'},
            sensitivity=Sensitivity.HIGH,
            allowed_for_generation=True,
        ),
        ProfileFact(
            fact_id='demo-race',
            kind=FactKind.PERSONAL,
            payload={'category': 'demographic', 'label': 'race_ethnicity', 'value': 'Asian'},
            sensitivity=Sensitivity.HIGH,
            allowed_for_generation=True,
        ),
        ProfileFact(
            fact_id='demo-hispanic',
            kind=FactKind.PERSONAL,
            payload={'category': 'demographic', 'label': 'hispanic_ethnicity', 'value': 'No'},
            sensitivity=Sensitivity.HIGH,
            allowed_for_generation=True,
        ),
        ProfileFact(
            fact_id='demo-veteran',
            kind=FactKind.PERSONAL,
            payload={'category': 'demographic', 'label': 'veteran_status', 'value': 'I am not a protected veteran'},
            sensitivity=Sensitivity.HIGH,
            allowed_for_generation=True,
        ),
        ProfileFact(
            fact_id='demo-disability',
            kind=FactKind.PERSONAL,
            payload={'category': 'demographic', 'label': 'disability_status', 'value': 'No, I do not have a disability and have not had one in the past'},
            sensitivity=Sensitivity.HIGH,
            allowed_for_generation=True,
        ),
    ]

    gender_answer = await service.answer_question('Gender', facts, options=['Male', 'Female'])
    race_answer = await service.answer_question('Race / Ethnicity', facts, options=['Asian', 'Black or African American'])
    hispanic_answer = await service.answer_question('Are you Hispanic/Latino?', facts, options=['Yes', 'No', 'Decline To Self Identify'])
    veteran_answer = await service.answer_question('Veteran Status', facts, options=['I am not a protected veteran', "I don't wish to answer"])
    disability_answer = await service.answer_question('Disability Status', facts, options=['Yes', 'No, I do not have a disability and have not had one in the past'])

    assert gender_answer.answer == 'Male'
    assert race_answer.answer == 'Asian'
    assert hispanic_answer.answer == 'No'
    assert veteran_answer.answer == 'I am not a protected veteran'
    assert disability_answer.answer == 'No, I do not have a disability and have not had one in the past'


@pytest.mark.anyio
async def test_grounding_maps_sensitive_select_to_real_option() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='auth-3',
            kind=FactKind.AUTHORIZATION,
            payload={'is_authorized': True, 'requires_future_sponsorship': False},
            sensitivity=Sensitivity.MEDIUM,
        )
    ]

    answer = await service.answer_question(
        'Work Authorization',
        facts,
        options=[
            'I am authorized to work for any employer in the country in which this position is based.',
            "I require/will require Lyft's sponsorship to obtain work authorization in the country in which this position is based (e.g. H-1B, TN, etc.)",
            'My status to work in the country in which this position is based is unknown.',
        ],
    )

    assert answer.answer == 'I am authorized to work for any employer in the country in which this position is based.'
    assert answer.selected_option_values == [answer.answer]


@pytest.mark.anyio
async def test_grounding_answers_current_employer_contact_as_no() -> None:
    service = GroundingService()

    answer = await service.answer_question(
        'May we contact your current employer?',
        [],
        options=['Yes', 'No'],
    )

    assert answer.answer == 'No'
    assert answer.verification_status == VerificationStatus.REVIEW_REQUIRED


@pytest.mark.anyio
async def test_grounding_uses_contact_name_for_certification_prompt() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='contact-2',
            kind=FactKind.CONTACT,
            payload={'name': 'Jordan Mercer'},
            sensitivity=Sensitivity.LOW,
        ),
        ProfileFact(
            fact_id='loc-1',
            kind=FactKind.LOCATION,
            payload={'region_code': 'TN'},
            sensitivity=Sensitivity.LOW,
        ),
    ]

    answer = await service.answer_question(
        'I certify that the facts set forth in this Application for Employment are true and complete to the best of my knowledge.',
        facts,
    )

    assert answer.answer == 'Jordan Mercer'
    assert answer.answer != 'TN'


@pytest.mark.anyio
async def test_grounding_prefers_explicit_contact_name_parts() -> None:
    service = GroundingService()
    facts = [
        ProfileFact(
            fact_id='contact-3',
            kind=FactKind.CONTACT,
            payload={
                'name': 'Jamie Lee Park',
                'first_name': 'Jamie Lee',
                'last_name': 'Park',
                'preferred_name': 'Jamie',
            },
            sensitivity=Sensitivity.LOW,
        )
    ]

    first_name = await service.answer_question('First Name', facts)
    last_name = await service.answer_question('Last Name', facts)
    preferred_name = await service.answer_question('Preferred First Name', facts)

    assert first_name.answer == 'Jamie Lee'
    assert last_name.answer == 'Park'
    assert preferred_name.answer == 'Jamie'


@pytest.mark.anyio
async def test_grounding_selects_single_acknowledgement_option() -> None:
    service = GroundingService()

    answer = await service.answer_question(
        'Please review the linked document:',
        [],
        options=['I acknowledge that I have read and understood the terms of the Candidate Privacy Notice.'],
    )

    assert answer.answer == 'I acknowledge that I have read and understood the terms of the Candidate Privacy Notice.'
    assert answer.selected_option_values == [answer.answer]
