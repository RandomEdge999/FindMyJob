"""Tests for confidence-based submission gating."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from findmyjob.apply.service import ApplicationService
from findmyjob.core.enums import (
    ArtifactKind,
    ApplicationMode,
    JobLifecycleStatus,
    PolicyMode,
    QuestionType,
    VerificationStatus,
)
from findmyjob.core.types import SubmissionGateReport


class _FakeQuestion:
    def __init__(self, prompt: str, required: bool = True, qtype: QuestionType = QuestionType.DETERMINISTIC):
        self.prompt_text = prompt
        self.required = required
        self.question_type = qtype


class _FakeAnswer:
    def __init__(self, text: str = "yes", confidence: float = 1.0, needs_user_input: bool = False, confidence_reason: str | None = "test"):
        self.candidate_answer = text
        self.confidence = confidence
        self.confidence_reason = confidence_reason
        self.needs_user_input = needs_user_input
        self.verification_status = VerificationStatus.VERIFIED


class TestLowConfidenceAnswers:
    def _make_service(self):
        return ApplicationService(MagicMock(), MagicMock())

    def test_high_confidence_not_flagged(self):
        svc = self._make_service()
        qa = [(_FakeQuestion("What is your name?"), _FakeAnswer("Alice", confidence=0.9))]
        assert svc.low_confidence_answers(qa) == []

    def test_low_confidence_flagged(self):
        svc = self._make_service()
        qa = [(_FakeQuestion("Are you authorized?"), _FakeAnswer("Yes", confidence=0.3))]
        result = svc.low_confidence_answers(qa)
        assert result == ["Are you authorized?"]

    def test_threshold_boundary(self):
        svc = self._make_service()
        qa = [(_FakeQuestion("Q1"), _FakeAnswer("A1", confidence=0.5))]
        assert svc.low_confidence_answers(qa, threshold=0.5) == []

    def test_below_threshold_boundary(self):
        svc = self._make_service()
        qa = [(_FakeQuestion("Q1"), _FakeAnswer("A1", confidence=0.49))]
        assert svc.low_confidence_answers(qa, threshold=0.5) == ["Q1"]

    def test_none_answer_skipped(self):
        svc = self._make_service()
        qa = [(_FakeQuestion("Q1"), None)]
        assert svc.low_confidence_answers(qa) == []

    def test_file_questions_skipped(self):
        svc = self._make_service()
        qa = [(_FakeQuestion("Resume", qtype=QuestionType.FILE), _FakeAnswer("path", confidence=0.0))]
        assert svc.low_confidence_answers(qa) == []

    def test_mixed_confidence_levels(self):
        svc = self._make_service()
        qa = [
            (_FakeQuestion("Q1"), _FakeAnswer("A1", confidence=0.9)),
            (_FakeQuestion("Q2"), _FakeAnswer("A2", confidence=0.2)),
            (_FakeQuestion("Q3"), _FakeAnswer("A3", confidence=0.7)),
        ]
        result = svc.low_confidence_answers(qa)
        assert result == ["Q2"]

    def test_legacy_answer_no_reason_not_flagged(self):
        """Answers with default confidence=0.0 but no confidence_reason are legacy and should not be flagged."""
        svc = self._make_service()
        qa = [(_FakeQuestion("Q1"), _FakeAnswer("A1", confidence=0.0, confidence_reason=None))]
        assert svc.low_confidence_answers(qa) == []


class TestSubmissionGateWithConfidence:
    def test_low_confidence_blocks_gate(self):
        gate = SubmissionGateReport(
            application_mode=ApplicationMode.AUTO_SUBMIT,
            source_policy=PolicyMode.HUMAN_IN_LOOP_SUBMIT,
            source_flow_valid=True,
            low_confidence_answers=["Are you authorized to work?"],
        )
        assert not gate.is_ready

    def test_no_low_confidence_allows_gate(self):
        gate = SubmissionGateReport(
            application_mode=ApplicationMode.AUTO_SUBMIT,
            source_policy=PolicyMode.HUMAN_IN_LOOP_SUBMIT,
            source_flow_valid=True,
        )
        assert gate.is_ready

    def test_gate_reports_all_blockers(self):
        gate = SubmissionGateReport(
            application_mode=ApplicationMode.AUTO_SUBMIT,
            source_policy=PolicyMode.HUMAN_IN_LOOP_SUBMIT,
            source_flow_valid=True,
            ungrounded_answers=["Q1"],
            low_confidence_answers=["Q2"],
            missing_required_fields=["Q3"],
        )
        assert not gate.is_ready
