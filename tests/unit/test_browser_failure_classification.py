"""Tests for Playwright browser failure classification and DOM analysis."""

from __future__ import annotations

from findmyjob.apply.browser import analyze_dom_snapshot, classify_submission_failure
from findmyjob.core.types import SubmissionEvidence


class TestClassifySubmissionFailure:
    def test_no_evidence(self):
        result = classify_submission_failure(None)
        assert result["failure_category"] == "unknown"
        assert result["retryable"] is False
        assert result["escalation_needed"] is True

    def test_login_wall(self):
        evidence = SubmissionEvidence(failure_reason="login_or_account_wall_detected")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "access_blocked"
        assert result["retryable"] is False
        assert result["escalation_needed"] is True

    def test_captcha(self):
        evidence = SubmissionEvidence(failure_reason="captcha_or_antibot_detected")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "captcha_blocked"
        assert result["retryable"] is False

    def test_rate_limited(self):
        evidence = SubmissionEvidence(failure_reason="429_rate_limit")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "rate_limited"
        assert result["retryable"] is True
        assert result["escalation_needed"] is False

    def test_playwright_unavailable(self):
        evidence = SubmissionEvidence(failure_reason="playwright_unavailable")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "infrastructure"
        assert result["retryable"] is False

    def test_playwright_runtime_blocked(self):
        evidence = SubmissionEvidence(failure_reason="playwright_runtime_blocked")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "infrastructure"
        assert result["retryable"] is False

    def test_timeout(self):
        evidence = SubmissionEvidence(failure_reason="playwright_timeout")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "timeout"
        assert result["retryable"] is True

    def test_missing_required_bindings(self):
        evidence = SubmissionEvidence(failure_reason="missing_required_bindings")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "form_binding"
        assert result["escalation_needed"] is True

    def test_location_autocomplete(self):
        evidence = SubmissionEvidence(failure_reason="location_autocomplete_failed")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "form_binding"
        assert result["retryable"] is True

    def test_validation_error(self):
        evidence = SubmissionEvidence(failure_reason="pre_submit_validation_failed")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "validation_error"

    def test_submit_button_missing(self):
        evidence = SubmissionEvidence(failure_reason="submit_button_missing")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "form_structure"

    def test_confirmation_not_detected(self):
        evidence = SubmissionEvidence(failure_reason="confirmation_not_detected")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "uncertain"
        assert result["retryable"] is False
        assert result["escalation_needed"] is False

    def test_unknown_reason(self):
        evidence = SubmissionEvidence(failure_reason="some_new_error_type")
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "unknown"
        assert result["failure_reason"] == "some_new_error_type"

    def test_empty_reason(self):
        evidence = SubmissionEvidence()
        result = classify_submission_failure(evidence)
        assert result["failure_category"] == "unknown"


class TestAnalyzeDomSnapshot:
    def test_login_wall_detected(self):
        html = '<html><body><h1>Sign in to apply</h1><form><input name="email"></form></body></html>'
        findings = analyze_dom_snapshot(html)
        assert findings["has_login_wall"] is True
        assert findings["has_form"] is True
        assert any("login_wall" in issue for issue in findings["detected_issues"])

    def test_captcha_detected(self):
        html = '<html><body><div class="g-recaptcha"></div><form></form></body></html>'
        findings = analyze_dom_snapshot(html)
        assert findings["has_captcha"] is True
        assert any("captcha" in issue for issue in findings["detected_issues"])

    def test_confirmation_page(self):
        html = '<html><body><div class="application-confirmation"><h1>Thank you for applying!</h1></div></body></html>'
        findings = analyze_dom_snapshot(html)
        assert findings["has_confirmation"] is True
        assert findings["has_login_wall"] is False
        assert findings["has_captcha"] is False

    def test_normal_form_page(self):
        html = '<html><body><form><input name="name"><button type="submit">Submit</button></form></body></html>'
        findings = analyze_dom_snapshot(html)
        assert findings["has_form"] is True
        assert findings["has_submit_button"] is True
        assert findings["has_login_wall"] is False
        assert findings["has_captcha"] is False
        assert findings["has_confirmation"] is False

    def test_empty_html(self):
        findings = analyze_dom_snapshot("")
        assert findings["has_form"] is False
        assert findings["has_login_wall"] is False
        assert findings["has_captcha"] is False
