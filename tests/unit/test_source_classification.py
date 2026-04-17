"""Tests for source automation classification by URL/domain/board family."""

from __future__ import annotations

from findmyjob.sources.classification import (
    AutomationTier,
    BoardClassification,
    BoardFamily,
    classify_job,
    classify_url,
    detect_board_family,
    detect_board_family_from_source_kind,
    is_auto_submittable,
    skip_reason_for_tier,
)


class TestDetectBoardFamily:
    def test_greenhouse_boards_api(self):
        family, method = detect_board_family("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        assert family == BoardFamily.GREENHOUSE
        assert "url_pattern" in method

    def test_greenhouse_boards(self):
        family, _ = detect_board_family("https://boards.greenhouse.io/acme")
        assert family == BoardFamily.GREENHOUSE

    def test_greenhouse_job_boards(self):
        family, _ = detect_board_family("https://job-boards.greenhouse.io/acme/jobs/123")
        assert family == BoardFamily.GREENHOUSE

    def test_lever_jobs(self):
        family, _ = detect_board_family("https://jobs.lever.co/acme/12345")
        assert family == BoardFamily.LEVER

    def test_lever_api(self):
        family, _ = detect_board_family("https://api.lever.co/v0/postings/acme")
        assert family == BoardFamily.LEVER

    def test_ashby_jobs(self):
        family, _ = detect_board_family("https://jobs.ashbyhq.com/acme")
        assert family == BoardFamily.ASHBY

    def test_workday(self):
        family, _ = detect_board_family("https://acme.myworkdayjobs.com/en-US/careers/job/12345")
        assert family == BoardFamily.WORKDAY

    def test_workday_wd_site(self):
        family, _ = detect_board_family("https://acme.wd5.myworkdaysite.com/en-US/External")
        assert family == BoardFamily.WORKDAY

    def test_icims(self):
        family, _ = detect_board_family("https://careers-acme.icims.com/jobs/12345")
        assert family == BoardFamily.ICIMS

    def test_taleo(self):
        family, _ = detect_board_family("https://acme.taleo.net/careersection/jobdetail.ftl")
        assert family == BoardFamily.TALEO

    def test_taleo_oraclecloud(self):
        family, _ = detect_board_family("https://acme.oraclecloud.com/hcmUI/CandidateExperience/en/jobs/12345")
        assert family == BoardFamily.TALEO

    def test_successfactors(self):
        family, _ = detect_board_family("https://acme.successfactors.com/career")
        assert family == BoardFamily.SUCCESSFACTORS

    def test_smartrecruiters(self):
        family, _ = detect_board_family("https://jobs.smartrecruiters.com/Acme/12345")
        assert family == BoardFamily.SMARTRECRUITERS

    def test_jobvite(self):
        family, _ = detect_board_family("https://jobs.jobvite.com/acme/job/12345")
        assert family == BoardFamily.JOBVITE

    def test_bamboohr(self):
        family, _ = detect_board_family("https://acme.bamboohr.com/careers/12345")
        assert family == BoardFamily.BAMBOOHR

    def test_unknown_domain(self):
        family, method = detect_board_family("https://custom-careers.example.com/apply")
        assert family == BoardFamily.UNKNOWN
        assert method == "no_match"

    def test_empty_url(self):
        family, method = detect_board_family("")
        assert family == BoardFamily.UNKNOWN
        assert method == "empty_url"


class TestDetectBoardFamilyFromSourceKind:
    def test_greenhouse(self):
        family, method = detect_board_family_from_source_kind("greenhouse")
        assert family == BoardFamily.GREENHOUSE
        assert "source_kind:greenhouse" == method

    def test_lever(self):
        family, _ = detect_board_family_from_source_kind("lever")
        assert family == BoardFamily.LEVER

    def test_ashby(self):
        family, _ = detect_board_family_from_source_kind("ashby")
        assert family == BoardFamily.ASHBY

    def test_unknown(self):
        family, method = detect_board_family_from_source_kind("custom_ats")
        assert family == BoardFamily.UNKNOWN
        assert "unknown" in method


class TestClassifyJob:
    def test_greenhouse_by_source_kind(self):
        result = classify_job(source_kind="greenhouse")
        assert result.board_family == BoardFamily.GREENHOUSE
        assert result.automation_tier == AutomationTier.AUTO_SUBMIT_SUPPORTED
        assert result.supports_auto_submit is True
        assert result.automation_skip_reason is None

    def test_lever_by_source_kind(self):
        result = classify_job(source_kind="lever")
        assert result.board_family == BoardFamily.LEVER
        assert result.automation_tier == AutomationTier.AUTO_SUBMIT_SUPPORTED
        assert result.supports_auto_submit is True

    def test_ashby_is_auto_submit_supported(self):
        result = classify_job(source_kind="ashby")
        assert result.board_family == BoardFamily.ASHBY
        assert result.automation_tier == AutomationTier.AUTO_SUBMIT_SUPPORTED
        assert result.supports_auto_submit is True
        assert result.automation_skip_reason is None

    def test_workday_is_unsupported(self):
        result = classify_job(apply_url="https://acme.myworkdayjobs.com/en-US/careers/job/12345")
        assert result.board_family == BoardFamily.WORKDAY
        assert result.automation_tier == AutomationTier.UNSUPPORTED_HIGH_FRICTION
        assert result.supports_auto_submit is False
        assert result.automation_skip_reason == "board_family_unsupported_high_friction"

    def test_unknown_domain_is_unsupported(self):
        result = classify_job(apply_url="https://careers.example.com/apply/12345")
        assert result.board_family == BoardFamily.UNKNOWN
        assert result.automation_tier == AutomationTier.UNSUPPORTED_HIGH_FRICTION
        assert result.supports_auto_submit is False

    def test_source_kind_takes_priority_over_url(self):
        result = classify_job(
            source_kind="greenhouse",
            apply_url="https://acme.myworkdayjobs.com/careers",
        )
        assert result.board_family == BoardFamily.GREENHOUSE
        assert result.automation_tier == AutomationTier.AUTO_SUBMIT_SUPPORTED

    def test_source_adapter_fallback(self):
        result = classify_job(source_adapter="greenhouse")
        assert result.board_family == BoardFamily.GREENHOUSE

    def test_apply_url_fallback_when_source_kind_unknown(self):
        result = classify_job(
            source_kind="custom_ats",
            apply_url="https://jobs.lever.co/acme/12345",
        )
        assert result.board_family == BoardFamily.LEVER
        assert result.automation_tier == AutomationTier.AUTO_SUBMIT_SUPPORTED

    def test_posting_url_fallback(self):
        result = classify_job(posting_url="https://boards.greenhouse.io/acme/jobs/123")
        assert result.board_family == BoardFamily.GREENHOUSE

    def test_confidence_full_for_known_source_kind(self):
        result = classify_job(source_kind="greenhouse")
        assert result.confidence == 1.0

    def test_confidence_partial_for_url_match(self):
        result = classify_job(apply_url="https://boards.greenhouse.io/acme/jobs/123")
        assert result.confidence == 0.9

    def test_confidence_zero_for_unknown(self):
        result = classify_job(apply_url="https://example.com/careers")
        assert result.confidence == 0.0


class TestClassifyUrl:
    def test_greenhouse_url(self):
        result = classify_url("https://boards.greenhouse.io/acme/jobs/123")
        assert result.board_family == BoardFamily.GREENHOUSE
        assert result.supports_auto_submit is True

    def test_workday_url(self):
        result = classify_url("https://acme.myworkdayjobs.com/en-US/careers/12345")
        assert result.board_family == BoardFamily.WORKDAY
        assert result.supports_auto_submit is False


class TestHelpers:
    def test_is_auto_submittable_true(self):
        classification = BoardClassification(
            board_family=BoardFamily.GREENHOUSE,
            automation_tier=AutomationTier.AUTO_SUBMIT_SUPPORTED,
            supports_auto_submit=True,
        )
        assert is_auto_submittable(classification) is True

    def test_is_auto_submittable_false(self):
        classification = BoardClassification(
            board_family=BoardFamily.WORKDAY,
            automation_tier=AutomationTier.UNSUPPORTED_HIGH_FRICTION,
            supports_auto_submit=False,
        )
        assert is_auto_submittable(classification) is False

    def test_skip_reason_for_auto_submit(self):
        assert skip_reason_for_tier(AutomationTier.AUTO_SUBMIT_SUPPORTED) is None

    def test_skip_reason_for_prepare_only(self):
        assert skip_reason_for_tier(AutomationTier.PREPARE_ONLY) == "board_family_prepare_only"

    def test_skip_reason_for_unsupported(self):
        assert skip_reason_for_tier(AutomationTier.UNSUPPORTED_HIGH_FRICTION) == "board_family_unsupported_high_friction"
