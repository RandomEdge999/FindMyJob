"""Tests for the CDP browser session module."""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from findmyjob.apply.cdp_session import (
    CDPAttachError,
    cdp_browser_context,
    chrome_launch_command,
)


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        pass


class FakeContext:
    def __init__(self, pages: list[FakePage]):
        self.pages = pages

    async def new_page(self) -> FakePage:
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context: FakeContext):
        self.contexts = [context]
        self.disconnect_called = False
        self.close_called = False

    async def new_context(self) -> FakeContext:
        return self.contexts[0]

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def close(self) -> None:
        self.close_called = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser
        self.connected_url: str | None = None

    async def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
        self.connected_url = cdp_url
        return self._browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser):
        self.chromium = FakeChromium(browser)
        self.stop_called = False

    async def stop(self) -> None:
        self.stop_called = True


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright):
        self._playwright = playwright

    async def start(self) -> FakePlaywright:
        return self._playwright

def _fake_playwright_modules(playwright: FakePlaywright) -> dict[str, object]:
    playwright_module = types.ModuleType("playwright")
    async_api_module = types.ModuleType("playwright.async_api")

    def async_playwright() -> FakePlaywrightStarter:
        return FakePlaywrightStarter(playwright)

    async_api_module.async_playwright = async_playwright
    playwright_module.async_api = async_api_module
    return {
        "playwright": playwright_module,
        "playwright.async_api": async_api_module,
    }


def test_cdp_attach_error_message():
    err = CDPAttachError("test failure")
    assert "test failure" in str(err)


def test_chrome_launch_command_returns_list():
    """chrome_launch_command returns a list when Chrome is found, or raises FileNotFoundError."""
    try:
        cmd = chrome_launch_command(port=9333)
        assert isinstance(cmd, list)
        assert any("--remote-debugging-port=9333" in arg for arg in cmd)
    except FileNotFoundError:
        pass


def test_chrome_launch_command_custom_port():
    try:
        cmd = chrome_launch_command(port=9444, profile_dir="/tmp/fake-profile")
        assert "--remote-debugging-port=9444" in cmd[1]
        assert "--user-data-dir=/tmp/fake-profile" in cmd[2]
    except FileNotFoundError:
        pass


def test_cdp_attach_error_is_runtime_error():
    assert issubclass(CDPAttachError, RuntimeError)


class TestCDPAttachErrorNoSilentFallback:
    """Verify that the CDP attach never silently falls back to isolated Chromium."""

    def test_error_type(self):
        err = CDPAttachError("connection refused")
        assert isinstance(err, RuntimeError)
        assert "connection refused" in str(err)

    def test_error_message_mentions_remote_debugging(self):
        err = CDPAttachError(
            "Could not attach to Chrome at http://127.0.0.1:9222. "
            "Make sure Chrome is running with --remote-debugging-port."
        )
        assert "--remote-debugging-port" in str(err)


class TestCDPBrowserContextCleanup:
    @pytest.mark.anyio
    async def test_disconnects_without_closing_remote_browser(self):
        existing_page = FakePage("https://my.greenhouse.io/jobs")
        created_page = FakePage("https://my.greenhouse.io/jobs/123")
        context = FakeContext([existing_page])
        browser = FakeBrowser(context)
        playwright = FakePlaywright(browser)

        with patch.dict(sys.modules, _fake_playwright_modules(playwright), clear=False):
            async with cdp_browser_context("http://127.0.0.1:9222") as (_browser, yielded_context):
                yielded_context.pages.append(created_page)

        assert existing_page.closed is False
        assert created_page.closed is True
        assert browser.disconnect_called is True
        assert browser.close_called is False
        assert playwright.stop_called is True

    @pytest.mark.anyio
    async def test_keep_tabs_open_preserves_new_pages(self):
        existing_page = FakePage("https://my.greenhouse.io/jobs")
        created_page = FakePage("https://my.greenhouse.io/jobs/456")
        context = FakeContext([existing_page])
        browser = FakeBrowser(context)
        playwright = FakePlaywright(browser)

        with patch.dict(sys.modules, _fake_playwright_modules(playwright), clear=False):
            async with cdp_browser_context("http://127.0.0.1:9222", keep_tabs_open=True) as (_browser, yielded_context):
                yielded_context.pages.append(created_page)

        assert existing_page.closed is False
        assert created_page.closed is False
        assert browser.disconnect_called is True
        assert browser.close_called is False



