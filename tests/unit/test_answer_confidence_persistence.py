"""Tests for answer confidence persistence through store_answer."""

from __future__ import annotations

import pytest

from findmyjob.core.enums import VerificationStatus
from findmyjob.core.types import GroundedAnswer


class TestGroundedAnswerConfidenceFields:
    def test_default_confidence_is_zero(self):
        answer = GroundedAnswer(question="test")
        assert answer.confidence == 0.0
        assert answer.reason is None

    def test_confidence_set_explicitly(self):
        answer = GroundedAnswer(question="test", confidence=0.85, reason="deterministic_match")
        assert answer.confidence == 0.85
        assert answer.reason == "deterministic_match"

    def test_confidence_clamped_to_range(self):
        with pytest.raises(Exception):
            GroundedAnswer(question="test", confidence=1.5)
        with pytest.raises(Exception):
            GroundedAnswer(question="test", confidence=-0.1)

    def test_selected_option_values_default_empty(self):
        answer = GroundedAnswer(question="test")
        assert answer.selected_option_values == []

    def test_full_answer_shape(self):
        answer = GroundedAnswer(
            question="Are you authorized to work in the US?",
            answer="Yes",
            selected_option_values=["yes"],
            confidence=0.85,
            reason="deterministic_match",
            verification_status=VerificationStatus.VERIFIED,
        )
        assert answer.confidence == 0.85
        assert answer.reason == "deterministic_match"
        assert answer.selected_option_values == ["yes"]
        assert not answer.needs_user_input
