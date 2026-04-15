"""Tests for the My Greenhouse page model."""
from __future__ import annotations

from pathlib import Path

import pytest

from findmyjob.apply.greenhouse_training import (
    JS_APPLY_ACTION,
    VALID_POSTED_WINDOWS,
    capture_dom_snapshot,
    capture_screenshot,
    extract_form_fields,
    extract_job_description,
    find_apply_url,
    find_company_job_page_url,
    harvest_visible_jobs,
    navigate_to_apply,
    navigate_to_company_job_page,
    navigate_to_job_view,
    set_posted_window,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLocator:
    def __init__(
        self,
        count: int = 0,
        text: str = "",
        attributes: dict[str, str | None] | None = None,
        children: list["FakeLocator"] | None = None,
        locator_map: dict[str, "FakeLocator"] | None = None,
        filter_result: "FakeLocator" | None = None,
        click_callback=None,
    ):
        self._count = count
        self._text = text
        self._attributes = attributes or {}
        self._children = children or []
        self._locator_map = locator_map or {}
        self._filter_result = filter_result
        self._click_callback = click_callback
        self._selected: list[str] = []
        self._clicked = False

    async def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self

    def nth(self, i: int):
        if self._children and i < len(self._children):
            return self._children[i]
        return self

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name)

    async def select_option(self, value: str) -> None:
        self._selected.append(value)

    async def click(self) -> None:
        self._clicked = True
        if self._click_callback is not None:
            await self._click_callback()

    def locator(self, selector: str) -> "FakeLocator":
        return self._locator_map.get(selector, FakeLocator(count=0))

    def filter(self, **kwargs) -> "FakeLocator":
        return self._filter_result or FakeLocator(count=0)

    async def evaluate(self, script: str) -> str:
        return "input"


class FakeKeyboard:
    async def press(self, key: str) -> None:
        pass


class FakePage:
    def __init__(
        self,
        selectors: dict[str, FakeLocator] | None = None,
        url: str = "https://my.greenhouse.io/jobs",
        title: str = "My Greenhouse Jobs",
        content_html: str = "<html><body></body></html>",
    ):
        self._selectors = selectors or {}
        self.url = url
        self._title = title
        self._content_html = content_html
        self._goto_urls: list[str] = []
        self.keyboard = FakeKeyboard()

    def locator(self, selector: str) -> FakeLocator:
        return self._selectors.get(selector, FakeLocator(count=0))

    async def goto(self, url: str, **kwargs) -> None:
        self._goto_urls.append(url)
        self.url = url

    async def wait_for_timeout(self, ms: int) -> None:
        pass

    async def wait_for_load_state(self, state: str, **kwargs) -> None:
        pass

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._content_html

    async def screenshot(self, path: str, **kwargs) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"fake_png")


# ---------------------------------------------------------------------------
# Tests: posted window values
# ---------------------------------------------------------------------------


class TestPostedWindowValues:
    def test_valid_values(self):
        assert VALID_POSTED_WINDOWS == (1, 5, 10, 30)

    @pytest.mark.anyio
    async def test_invalid_window_raises(self):
        page = FakePage()
        with pytest.raises(ValueError, match="posted_window must be one of"):
            await set_posted_window(page, 7)

    @pytest.mark.anyio
    async def test_valid_windows_all_accepted(self):
        for days in VALID_POSTED_WINDOWS:
            page = FakePage()
            await set_posted_window(page, days)
            assert f"posted_within={days}" in page._goto_urls[-1]


# ---------------------------------------------------------------------------
# Tests: job harvesting
# ---------------------------------------------------------------------------


class TestHarvestVisibleJobs:
    @pytest.mark.anyio
    async def test_empty_page_returns_empty(self):
        page = FakePage()
        jobs = await harvest_visible_jobs(page, max_jobs=5)
        assert jobs == []

    @pytest.mark.anyio
    async def test_jobs_returned_as_dicts(self):
        row_link = FakeLocator(count=1, text="Software Engineer", attributes={"href": "/jobs/123"})
        row = FakeLocator(
            count=1,
            text="Software Engineer\tAcme Corp\tNew York\t2 days ago",
            locator_map={"a[href*='/job'], a[href*='/jobs']": row_link},
        )
        rows = FakeLocator(count=1, children=[row])
        page = FakePage(selectors={
            "table tbody tr, [class*='job-row'], [class*='job_row'], [data-job-id]": rows,
        })
        jobs = await harvest_visible_jobs(page, max_jobs=5)
        assert jobs[0]["url"] == "https://my.greenhouse.io/jobs/123"
        assert jobs[0]["title"] == "Software Engineer"


# ---------------------------------------------------------------------------
# Tests: navigation
# ---------------------------------------------------------------------------


class TestNavigateToJobView:
    @pytest.mark.anyio
    async def test_navigates_and_captures(self):
        page = FakePage()
        capture = await navigate_to_job_view(page, "https://my.greenhouse.io/jobs/456")
        assert capture.url == "https://my.greenhouse.io/jobs/456"
        assert capture.page_title == "My Greenhouse Jobs"


class TestFindApplyUrl:
    @pytest.mark.anyio
    async def test_no_apply_link(self):
        page = FakePage()
        result = await find_apply_url(page)
        assert result is None

    @pytest.mark.anyio
    async def test_apply_link_found_and_resolved(self):
        link = FakeLocator(count=1, attributes={"href": "#application"})
        page = FakePage(
            selectors={"a[href*='#application']": link},
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
        result = await find_apply_url(page)
        assert result == "https://boards.greenhouse.io/acme/jobs/123#application"

    @pytest.mark.anyio
    async def test_js_apply_control_returns_sentinel(self):
        button = FakeLocator(count=1)
        page = FakePage(selectors={"button:has-text('Apply')": button})
        result = await find_apply_url(page)
        assert result == JS_APPLY_ACTION


class TestFindCompanyJobPageUrl:
    @pytest.mark.anyio
    async def test_no_company_link(self):
        page = FakePage()
        result = await find_company_job_page_url(page)
        assert result is None

    @pytest.mark.anyio
    async def test_relative_company_link_is_resolved(self):
        page = FakePage(
            selectors={"a:has-text('View job post')": FakeLocator(count=1, attributes={"href": "/public/jobs/123"})},
            url="https://my.greenhouse.io/jobs/123",
        )
        result = await find_company_job_page_url(page)
        assert result == "https://my.greenhouse.io/public/jobs/123"


class TestApplyNavigation:
    @pytest.mark.anyio
    async def test_company_navigation_uses_resolved_url(self):
        page = FakePage(url="https://my.greenhouse.io/jobs/123")
        capture = await navigate_to_company_job_page(page, "/public/jobs/123")
        assert page._goto_urls[-1] == "https://my.greenhouse.io/public/jobs/123"
        assert capture.url == "https://my.greenhouse.io/public/jobs/123"

    @pytest.mark.anyio
    async def test_relative_apply_url_uses_current_page_domain(self):
        page = FakePage(url="https://boards.greenhouse.io/acme/jobs/123")
        capture = await navigate_to_apply(page, "#application")
        assert page._goto_urls[-1] == "https://boards.greenhouse.io/acme/jobs/123#application"
        assert capture.url == "https://boards.greenhouse.io/acme/jobs/123#application"

    @pytest.mark.anyio
    async def test_js_apply_clicks_control(self):
        page = FakePage(url="https://boards.greenhouse.io/acme/jobs/123")

        async def _update_url():
            page.url = "https://boards.greenhouse.io/acme/jobs/123#application"

        button = FakeLocator(count=1, click_callback=_update_url)
        page._selectors["button:has-text('Apply')"] = button
        capture = await navigate_to_apply(page, JS_APPLY_ACTION)
        assert button._clicked is True
        assert capture.url == "https://boards.greenhouse.io/acme/jobs/123#application"


# ---------------------------------------------------------------------------
# Tests: form field extraction
# ---------------------------------------------------------------------------


class TestExtractFormFields:
    @pytest.mark.anyio
    async def test_empty_form(self):
        page = FakePage()
        fields = await extract_form_fields(page)
        assert fields == []


# ---------------------------------------------------------------------------
# Tests: captures
# ---------------------------------------------------------------------------


class TestCaptures:
    @pytest.mark.anyio
    async def test_screenshot(self, tmp_path):
        page = FakePage()
        path = await capture_screenshot(page, tmp_path, prefix="test")
        assert path.exists()
        assert path.suffix == ".png"

    @pytest.mark.anyio
    async def test_dom_snapshot(self, tmp_path):
        page = FakePage()
        path = await capture_dom_snapshot(page, tmp_path, prefix="test")
        assert path.exists()
        assert path.suffix == ".html"
        content = path.read_text()
        assert "<html>" in content


class TestExtractJobDescription:
    @pytest.mark.anyio
    async def test_fallback_to_body(self):
        page = FakePage(content_html="<html><body>A very long job description goes here " + "x" * 100 + "</body></html>")
        page._selectors["body"] = FakeLocator(count=1, text="A very long job description goes here " + "x" * 100)
        desc = await extract_job_description(page)
        assert isinstance(desc, str)
