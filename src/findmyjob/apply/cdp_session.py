"""CDP-backed browser session for attaching to a user's logged-in Chrome.

This module provides a Playwright CDP attachment to an existing Chrome
instance. It is intentionally separate from the isolated headless
Chromium path used by the autonomous submit flow.

Important: cleanup disconnects Playwright without closing Chrome.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page


_DEFAULT_CDP_URL = "http://127.0.0.1:9222"
_MY_GREENHOUSE_JOBS_URL = "https://my.greenhouse.io/jobs"


class CDPAttachError(RuntimeError):
    """Raised when CDP attachment fails."""


def _stable_page_id(page: "Page") -> str:
    return str(id(page))


def _normalize_url(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _context_for_my_greenhouse(browser: "Browser") -> "BrowserContext":
    if not browser.contexts:
        raise CDPAttachError(
            "Attached Chrome did not expose any browser contexts. "
            "Start a normal Chrome window with remote debugging enabled so Find My Job can reuse your logged-in profile."
        )
    for context in browser.contexts:
        for page in context.pages:
            if _normalize_url(page.url).startswith(_MY_GREENHOUSE_JOBS_URL):
                return context
    return browser.contexts[0]


@asynccontextmanager
async def cdp_browser_context(
    cdp_url: str = _DEFAULT_CDP_URL,
    *,
    keep_tabs_open: bool = False,
) -> AsyncIterator[tuple["Browser", "BrowserContext"]]:
    """Connect to an existing Chrome over CDP and yield ``(browser, context)``.

    This never falls back to launching isolated Chromium. Cleanup only
    disconnects Playwright from the attached Chrome instance.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        await playwright.stop()
        raise CDPAttachError(
            f"Could not attach to Chrome at {cdp_url}. "
            "Make sure Chrome is already running with --remote-debugging-port. "
            f"Original error: {exc}"
        ) from exc

    context = _context_for_my_greenhouse(browser)
    pre_existing_pages = {_stable_page_id(page) for page in context.pages}

    try:
        yield browser, context
    finally:
        if not keep_tabs_open:
            for page in list(context.pages):
                if _stable_page_id(page) in pre_existing_pages:
                    continue
                try:
                    await page.close()
                except Exception:
                    pass
        try:
            await browser.disconnect()
        except Exception:
            pass
        await playwright.stop()


async def find_or_open_tab(
    context: "BrowserContext",
    target_url: str = _MY_GREENHOUSE_JOBS_URL,
) -> "Page":
    """Reuse an existing tab for *target_url* when possible, otherwise open one."""
    normalized_target = _normalize_url(target_url)
    for page in context.pages:
        page_url = _normalize_url(page.url)
        if page_url == normalized_target or page_url.startswith(normalized_target):
            try:
                await page.bring_to_front()
            except Exception:
                pass
            return page

    page = await context.new_page()
    await page.goto(target_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    return page


# ---------------------------------------------------------------------------
# Chrome launch helpers
# ---------------------------------------------------------------------------


def chrome_launch_command(
    port: int = 9222,
    profile_dir: str | None = None,
    start_url: str | None = _MY_GREENHOUSE_JOBS_URL,
) -> list[str]:
    """Return a Chrome command line for CDP attach mode."""
    chrome = _find_chrome_executable()
    if chrome is None:
        raise FileNotFoundError(
            "Could not locate Chrome. Start Chrome manually with --remote-debugging-port=9222 and then rerun the training command."
        )

    resolved_profile = profile_dir
    if resolved_profile is None and platform.system() == "Windows":
        resolved_profile = str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
    elif resolved_profile is None and platform.system() == "Darwin":
        resolved_profile = str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")

    command = [chrome, f"--remote-debugging-port={port}"]
    if resolved_profile:
        command.append(f"--user-data-dir={resolved_profile}")
    if start_url:
        command.append(start_url)
    return command


def chrome_debug_instructions(port: int = 9222) -> str:
    """Return human-readable instructions for starting Chrome in debug mode."""
    chrome = _find_chrome_executable() or "chrome.exe"
    windows_command = (
        f'"{chrome}" --remote-debugging-port={port} '
        f'--user-data-dir="%LOCALAPPDATA%\\Google\\Chrome\\User Data" '
        f'{_MY_GREENHOUSE_JOBS_URL}'
    )
    lines = [
        "Start Chrome with remote debugging enabled before running attach-mode training:",
        "",
        "Windows:",
        f"  {windows_command}",
    ]
    system = platform.system()
    if system == "Darwin":
        lines.extend(
            [
                "",
                "macOS:",
                f'  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port={port} {_MY_GREENHOUSE_JOBS_URL}',
            ]
        )
    elif system not in {"Windows", "Darwin"}:
        lines.extend(
            [
                "",
                "Linux:",
                f"  google-chrome --remote-debugging-port={port} {_MY_GREENHOUSE_JOBS_URL}",
            ]
        )
    lines.extend(
        [
            "",
            "Notes:",
            "  - Close existing Chrome windows first if you want to reuse your main logged-in profile.",
            "  - Find My Job attaches to that running Chrome session; it does not launch a private fallback browser.",
            "  - Training mode never bypasses login, captcha, or account walls.",
            f"  - Then run: fmj greenhouse train --cdp-url http://127.0.0.1:{port} --posted-window 10 --batch-size 5",
        ]
    )
    return "\n".join(lines)


def launch_chrome_debug(
    port: int = 9222,
    profile_dir: str | None = None,
    start_url: str | None = _MY_GREENHOUSE_JOBS_URL,
) -> subprocess.Popen[bytes]:
    """Launch Chrome in the background with remote debugging enabled."""
    command = chrome_launch_command(port=port, profile_dir=profile_dir, start_url=start_url)
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _find_chrome_executable() -> str | None:
    """Best-effort Chrome binary lookup."""
    which = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if which:
        return which

    if platform.system() == "Windows":
        candidates = [
            Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    if platform.system() == "Darwin":
        app = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if app.exists():
            return str(app)

    return None


