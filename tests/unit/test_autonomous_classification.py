"""Tests for autonomous loop classification integration.

Verifies that:
- Only auto_submit_supported boards enter the submit pipeline
- Hard gate now uses classification instead of hard-coded greenhouse check
- Classification metadata is persisted in autonomous notes
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from findmyjob.personal.autonomous import _hard_gate_reasons, _update_autonomous_notes
from findmyjob.sources.classification import AutomationTier, BoardClassification, BoardFamily


def _make_job(*, source_adapter="greenhouse", source_kind="greenhouse", apply_url="https://boards.greenhouse.io/acme/jobs/123", posting_url=None, notes=None):
    """Create a minimal mock JobPosting for gate checking."""
    job = MagicMock()
    job.source_adapter = source_adapter
    job.source_kind = source_kind
    job.apply_url = apply_url
    job.posting_url = posting_url or apply_url
    job.title = "Software Engineer"
    job.description = "Build things."
    job.normalized_description = "Build things."
    job.notes = notes or {}
    job.id = "job-123"
    return job


def _make_facts(*, is_authorized=True, requires_future_sponsorship=False):
    fact = MagicMock()
    fact.kind = MagicMock()
    fact.kind.value = "authorization"
    fact.disallowed = False
    fact.payload = {
        "is_authorized": is_authorized,
        "requires_future_sponsorship": requires_future_sponsorship,
    }
    return [fact]


class TestHardGateWithClassification:
    def test_greenhouse_job_passes_hard_gate(self):
        job = _make_job(source_kind="greenhouse")
        reasons = _hard_gate_reasons(job, _make_facts())
        assert reasons == []

    def test_lever_job_passes_hard_gate(self):
        job = _make_job(source_adapter="lever", source_kind="lever", apply_url="https://jobs.lever.co/acme/123")
        reasons = _hard_gate_reasons(job, _make_facts())
        assert reasons == []

    def test_workday_job_fails_hard_gate(self):
        job = _make_job(source_adapter="workday", source_kind="workday", apply_url="https://acme.myworkdayjobs.com/careers/123")
        reasons = _hard_gate_reasons(job, _make_facts())
        assert len(reasons) == 1
        assert "unsupported" in reasons[0].lower() or "high_friction" in reasons[0].lower()

    def test_ashby_job_passes_hard_gate(self):
        job = _make_job(source_adapter="ashby", source_kind="ashby", apply_url="https://jobs.ashbyhq.com/acme/123")
        reasons = _hard_gate_reasons(job, _make_facts())
        assert reasons == []

    def test_unknown_source_fails_hard_gate(self):
        job = _make_job(source_adapter="custom", source_kind="custom", apply_url="https://careers.example.com/apply")
        reasons = _hard_gate_reasons(job, _make_facts())
        assert len(reasons) == 1

    def test_missing_apply_url_fails(self):
        job = _make_job(apply_url="")
        reasons = _hard_gate_reasons(job, _make_facts())
        assert "missing_apply_url" in reasons


class TestAutonomousNotesClassification:
    def test_classification_metadata_persisted_in_notes(self):
        job = _make_job()
        job.notes = {}
        classification = BoardClassification(
            board_family=BoardFamily.GREENHOUSE,
            automation_tier=AutomationTier.AUTO_SUBMIT_SUPPORTED,
            supports_auto_submit=True,
            detection_method="source_kind:greenhouse",
            confidence=1.0,
        )
        _update_autonomous_notes(job, classification=classification)
        autonomous = job.notes.get("autonomous", {})
        assert autonomous["board_family"] == "greenhouse"
        assert autonomous["automation_tier"] == "auto_submit_supported"
        assert autonomous["supports_auto_submit"] is True
        assert autonomous["classification_method"] == "source_kind:greenhouse"

    def test_classification_skip_reason_persisted(self):
        job = _make_job()
        job.notes = {}
        classification = BoardClassification(
            board_family=BoardFamily.WORKDAY,
            automation_tier=AutomationTier.UNSUPPORTED_HIGH_FRICTION,
            supports_auto_submit=False,
            automation_skip_reason="board_family_unsupported_high_friction",
            detection_method="url_pattern:workday",
        )
        _update_autonomous_notes(job, classification=classification)
        autonomous = job.notes.get("autonomous", {})
        assert autonomous["automation_skip_reason"] == "board_family_unsupported_high_friction"

    def test_none_job_is_safe(self):
        _update_autonomous_notes(None, classification=BoardClassification())
