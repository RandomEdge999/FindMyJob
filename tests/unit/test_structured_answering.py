"""Tests for structured option answering and uncertainty behavior.

Verifies that:
- SELECT questions choose from actual options, not free-text
- Uncertain answers have confidence=0 and no answer
- Deterministic fact matching populates selected_option_values
- Narrative answers populate confidence and reason
"""

from __future__ import annotations

import pytest

from findmyjob.core.enums import QuestionType, VerificationStatus
from findmyjob.core.types import ProfileFact
from findmyjob.grounding.service import GroundingService


def _fact(fact_id: str, kind: str, payload: dict) -> ProfileFact:
    return ProfileFact(fact_id=fact_id, kind=kind, payload=payload)


@pytest.fixture
def grounding():
    return GroundingService(router=None)


@pytest.fixture
def facts():
    return [
        _fact("f-email", "contact", {"email": "alice@example.com", "phone": "555-1234", "name": "Alice Smith"}),
        _fact("f-auth", "authorization", {"is_authorized": True, "requires_future_sponsorship": False}),
        _fact("f-loc", "location", {"display": "New York, NY", "city": "New York", "country_code": "US"}),
        _fact("f-work", "work", {
            "company": "Acme Inc",
            "title": "Senior Engineer",
            "summary": "Built distributed systems at scale",
            "bullets": ["Led team of 5", "Reduced latency by 40%"],
        }),
        _fact("f-skill", "skill", {"name": "Python", "years": 8}),
    ]


class TestSelectQuestionAnswering:
    @pytest.mark.anyio
    async def test_deterministic_option_match_from_facts(self, grounding, facts):
        answer = await grounding.answer_question(
            "What city are you located in?",
            facts,
            options=["New York, NY", "San Francisco, CA", "Austin, TX"],
        )
        assert answer.answer == "New York, NY"
        assert answer.selected_option_values == ["New York, NY"]
        assert answer.confidence > 0
        assert "f-loc" in answer.used_fact_ids

    @pytest.mark.anyio
    async def test_no_match_returns_uncertainty(self, grounding, facts):
        answer = await grounding.answer_question(
            "What is your preferred office?",
            facts,
            options=["London", "Tokyo", "Berlin"],
        )
        # No option matches any fact, should return uncertainty
        assert answer.answer is None
        assert answer.confidence == 0.0
        assert answer.reason == "no_option_matched_profile"

    @pytest.mark.anyio
    async def test_boolean_select_handled_correctly(self, grounding, facts):
        answer = await grounding.answer_question(
            "Are you authorized to work in the US?",
            facts,
            options=["Yes", "No"],
        )
        # "authorized" is in SENSITIVE_QUESTION_KEYWORDS, so classified as SENSITIVE
        # The key point is it gets answered from facts, not treated as unconstrained SELECT
        assert answer.answer in {"Yes", "No"}
        assert answer.question_type in {QuestionType.BOOLEAN, QuestionType.SENSITIVE}

    @pytest.mark.anyio
    async def test_select_does_not_return_free_text(self, grounding, facts):
        """SELECT answers must be from the provided options, not unconstrained prose."""
        answer = await grounding.answer_question(
            "What is your experience level?",
            facts,
            options=["Junior", "Mid-level", "Senior", "Staff"],
        )
        if answer.answer is not None:
            assert answer.answer in ["Junior", "Mid-level", "Senior", "Staff"]


class TestAnswerOutputShape:
    @pytest.mark.anyio
    async def test_answer_memory_has_full_shape(self, grounding, facts):
        # canonicalize_question uses slugify which produces "what-is-your-email"
        canonical = grounding.canonicalize_question("What is your email?")
        answer = await grounding.answer_question(
            "What is your email?",
            facts,
            answer_memory=[{
                "canonical_question": canonical,
                "answer_text": "alice@example.com",
                "grounded_fact_ids": ["f-email"],
                "approved": True,
                "context_constraints": {},
            }],
            memory_context={},
        )
        assert answer.answer == "alice@example.com"
        assert answer.confidence == 1.0
        assert answer.reason == "answer_memory_hit"

    @pytest.mark.anyio
    async def test_deterministic_answer_has_confidence(self, grounding, facts):
        answer = await grounding.answer_question("What is your email address?", facts)
        assert answer.answer == "alice@example.com"
        assert answer.question_type == QuestionType.DETERMINISTIC

    @pytest.mark.anyio
    async def test_narrative_answer_has_confidence_and_reason(self, grounding, facts):
        answer = await grounding.answer_question(
            "Describe your experience with distributed systems",
            facts,
        )
        assert answer.question_type == QuestionType.NARRATIVE
        assert answer.confidence > 0
        assert answer.reason is not None
        assert "narrative" in answer.reason or "fact" in answer.reason

    @pytest.mark.anyio
    async def test_select_uncertainty_has_zero_confidence(self, grounding, facts):
        answer = await grounding.answer_question(
            "What framework do you prefer?",
            facts,
            options=["Django", "Rails", "Spring"],
        )
        # Python fact won't match these framework options
        assert answer.confidence == 0.0 or answer.answer in ["Django", "Rails", "Spring"]


class TestSensitiveQuestions:
    @pytest.mark.anyio
    async def test_authorization_question(self, grounding, facts):
        answer = await grounding.answer_question(
            "Are you authorized to work in the US?",
            facts,
        )
        assert answer.answer == "Yes"
        assert answer.question_type in {QuestionType.BOOLEAN, QuestionType.SENSITIVE}

    @pytest.mark.anyio
    async def test_sponsorship_question(self, grounding, facts):
        answer = await grounding.answer_question(
            "Will you now or in the future require visa sponsorship?",
            facts,
        )
        assert answer.answer == "No"
