"""Tests for retryable submission failure classification and retry decisions."""

from __future__ import annotations

from findmyjob.apply.browser import classify_submission_failure, analyze_dom_snapshot
from findmyjob.core.types import SubmissionEvidence


class TestRetryDecisionLogic:
    """Test that classify_submission_failure correctly identifies retryable failures."""

    def test_rate_limit_is_retryable(self):
        evidence = SubmissionEvidence(failure_reason="429_rate_limit")
        result = classify_submission_failure(evidence)
        assert result["retryable"] is True
        assert result["failure_category"] == "rate_limited"

    def test_timeout_is_retryable(self):
        evidence = SubmissionEvidence(failure_reason="timeout_waiting_for_submit")
        result = classify_submission_failure(evidence)
        assert result["retryable"] is True
        assert result["failure_category"] == "timeout"

    def test_location_autocomplete_is_retryable(self):
        evidence = SubmissionEvidence(failure_reason="location_autocomplete_failed")
        result = classify_submission_failure(evidence)
        assert result["retryable"] is True

    def test_login_wall_not_retryable(self):
        evidence = SubmissionEvidence(failure_reason="login_required")
        result = classify_submission_failure(evidence)
        assert result["retryable"] is False
        assert result["escalation_needed"] is True

    def test_captcha_not_retryable(self):
        evidence = SubmissionEvidence(failure_reason="captcha_detected")
        result = classify_submission_failure(evidence)
        assert result["retryable"] is False

    def test_infrastructure_not_retryable(self):
        evidence = SubmissionEvidence(failure_reason="playwright_unavailable")
        result = classify_submission_failure(evidence)
        assert result["retryable"] is False
        assert result["escalation_needed"] is True


class TestDomSnapshotRetryGuard:
    """Test that DOM analysis can prevent retry for login walls and captchas."""

    def test_login_wall_in_dom_prevents_retry(self):
        html = '<div class="login-form"><h2>Sign in to continue</h2><input type="password"></div>'
        analysis = analyze_dom_snapshot(html)
        assert analysis["has_login_wall"] is True

    def test_captcha_in_dom_prevents_retry(self):
        html = '<div class="g-recaptcha" data-sitekey="abc123"></div><form id="application"></form>'
        analysis = analyze_dom_snapshot(html)
        assert analysis["has_captcha"] is True

    def test_confirmation_in_dom_prevents_retry(self):
        html = '<div class="success"><h1>Thank you for applying!</h1></div>'
        analysis = analyze_dom_snapshot(html)
        assert analysis["has_confirmation"] is True

    def test_normal_form_allows_retry(self):
        html = '<form id="application"><input type="text" name="name"><button type="submit">Apply</button></form>'
        analysis = analyze_dom_snapshot(html)
        assert analysis["has_login_wall"] is False
        assert analysis["has_captcha"] is False
        assert analysis["has_confirmation"] is False
        assert analysis["has_form"] is True
        assert analysis["has_submit_button"] is True
