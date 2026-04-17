"""Tests for HTML content-based board family detection."""

from __future__ import annotations

from findmyjob.sources.classification import (
    BoardFamily,
    classify_job,
    detect_board_family_from_html,
)


class TestDetectBoardFamilyFromHtml:
    def test_greenhouse_signals(self):
        html = '<div data-greenhouse="true"><script src="https://boards.greenhouse.io/embed/job.js"></script></div>'
        family, method = detect_board_family_from_html(html)
        assert family == BoardFamily.GREENHOUSE
        assert "html_content:greenhouse" == method

    def test_lever_signals(self):
        html = '<div class="lever-jobs-container"><a href="https://jobs.lever.co/company">Apply</a></div>'
        family, method = detect_board_family_from_html(html)
        assert family == BoardFamily.LEVER
        assert method == "html_content:lever"

    def test_workday_signals(self):
        html = '<div class="wd-uiautomation"><script>WORKDAY_CONFIG = {}</script></div>'
        family, method = detect_board_family_from_html(html)
        assert family == BoardFamily.WORKDAY

    def test_icims_signals(self):
        html = '<div data-icims="true" class="icims_content">Apply via iCIMS</div>'
        family, method = detect_board_family_from_html(html)
        assert family == BoardFamily.ICIMS

    def test_ashby_signals(self):
        html = '<div class="ashby-job-posting"><script src="https://jobs.ashbyhq.com/embed.js"></script></div>'
        family, method = detect_board_family_from_html(html)
        assert family == BoardFamily.ASHBY

    def test_single_signal_not_enough(self):
        html = '<a href="https://greenhouse.io">Visit Greenhouse</a>'
        family, _ = detect_board_family_from_html(html)
        # Only 1 signal match — not enough (threshold is 2)
        assert family == BoardFamily.UNKNOWN

    def test_empty_html(self):
        family, method = detect_board_family_from_html("")
        assert family == BoardFamily.UNKNOWN
        assert method == "empty_html"

    def test_no_signals(self):
        html = '<html><body><h1>Generic career page</h1></body></html>'
        family, method = detect_board_family_from_html(html)
        assert family == BoardFamily.UNKNOWN
        assert method == "no_html_match"

    def test_smartrecruiters_signals(self):
        html = '<iframe src="https://jobs.smartrecruiters.com/company/job"><div class="smartrecruiters-widget"></div></iframe>'
        family, _ = detect_board_family_from_html(html)
        assert family == BoardFamily.SMARTRECRUITERS

    def test_taleo_signals(self):
        html = '<div data-taleo="true"><script src="https://company.taleo.net/embed.js"></script></div>'
        family, _ = detect_board_family_from_html(html)
        assert family == BoardFamily.TALEO


class TestClassifyJobWithHtml:
    def test_html_fallback_when_url_unknown(self):
        result = classify_job(
            source_kind="custom",
            apply_url="https://careers.acme.com/job/123",
            page_html='<div data-greenhouse="true"><script src="https://boards.greenhouse.io/embed.js"></script></div>',
        )
        assert result.board_family == BoardFamily.GREENHOUSE
        assert result.detection_method == "html_content:greenhouse"

    def test_url_takes_priority_over_html(self):
        result = classify_job(
            apply_url="https://boards.greenhouse.io/acme/jobs/123",
            page_html='<div class="lever-jobs-container"><a href="https://jobs.lever.co">Lever</a></div>',
        )
        assert result.board_family == BoardFamily.GREENHOUSE
        assert "url_pattern" in result.detection_method

    def test_source_kind_takes_priority_over_html(self):
        result = classify_job(
            source_kind="lever",
            page_html='<div data-greenhouse="true"><script src="https://boards.greenhouse.io/embed.js"></script></div>',
        )
        assert result.board_family == BoardFamily.LEVER
