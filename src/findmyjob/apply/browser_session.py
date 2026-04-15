from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def cdp_port(browser_cdp_url: str | None) -> int:
    parsed = urlparse(browser_cdp_url or "http://127.0.0.1:9222")
    return int(parsed.port or 9222)


def attachable_browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    roots = [
        Path(os.environ.get("PROGRAMFILES") or ""),
        Path(os.environ.get("PROGRAMFILES(X86)") or ""),
        Path(os.environ.get("LOCALAPPDATA") or ""),
    ]
    suffixes = [
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Chromium/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
    ]
    for root in roots:
        if not str(root):
            continue
        for suffix in suffixes:
            candidate = root / suffix
            if candidate.exists():
                candidates.append(candidate)
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _is_cdp_listening(port: int) -> bool:
    """Quick check whether anything answers on the CDP port."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/json/version")
        resp = conn.getresponse()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


def launch_attachable_browser(
    *,
    browser_cdp_url: str | None,
    profile_dir: Path,
    start_url: str = "about:blank",
) -> bool:
    import time

    port = cdp_port(browser_cdp_url)
    # Only create the dir if it doesn't already exist (real Chrome profiles
    # already exist; creating sub-dirs inside them is harmless but we should
    # not fail if the dir is present).
    profile_dir.mkdir(parents=True, exist_ok=True)
    for executable in attachable_browser_candidates():
        try:
            subprocess.Popen(
                [
                    str(executable),
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--new-window",
                    start_url or "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for Chrome to actually start listening on the CDP port
            for _ in range(12):
                time.sleep(1.5)
                if _is_cdp_listening(port):
                    return True
            # Process started but never listened — profile may be locked
            return False
        except Exception:
            continue
    return False


async def connect_attached_browser(
    playwright: Any,
    *,
    browser_cdp_url: str,
    accept_downloads: bool = False,
) -> dict[str, Any]:
    browser = await playwright.chromium.connect_over_cdp(browser_cdp_url)
    existing_contexts = list(browser.contexts)
    if existing_contexts:
        context = existing_contexts[0]
    else:
        context = await browser.new_context(accept_downloads=accept_downloads)
    page = await context.new_page()
    return {
        "browser": browser,
        "context": context,
        "page": page,
        "attached": True,
        "created_context": not existing_contexts,
        "close_browser": False,
    }


async def open_browser_session(
    playwright: Any,
    *,
    browser_mode: str = "headless",
    browser_attach_enabled: bool = False,
    browser_cdp_url: str | None = None,
    launch_if_missing: bool = False,
    profile_dir: Path | None = None,
    start_url: str = "about:blank",
    accept_downloads: bool = False,
) -> dict[str, Any]:
    attach_enabled = bool(browser_attach_enabled and browser_mode == "attached" and browser_cdp_url)
    if attach_enabled:
        # Step 1: try to connect to an already-running Chrome with CDP enabled
        try:
            return await connect_attached_browser(
                playwright,
                browser_cdp_url=str(browser_cdp_url),
                accept_downloads=accept_downloads,
            )
        except Exception:
            pass
        # Step 2: launch Chrome with the operator's profile + CDP flag
        if launch_if_missing and profile_dir is not None:
            launched = launch_attachable_browser(
                browser_cdp_url=browser_cdp_url,
                profile_dir=profile_dir,
                start_url=start_url,
            )
            if launched:
                last_error: Exception | None = None
                for _ in range(15):
                    await asyncio.sleep(1.5)
                    try:
                        return await connect_attached_browser(
                            playwright,
                            browser_cdp_url=str(browser_cdp_url),
                            accept_downloads=accept_downloads,
                        )
                    except Exception as exc:
                        last_error = exc
                        continue
                raise RuntimeError(
                    f"Chrome launched but CDP connection to {browser_cdp_url} failed "
                    f"after 15 retries. Close any existing Chrome windows and retry. "
                    f"Last error: {last_error}"
                )
            raise RuntimeError(
                "Could not launch Chrome with CDP. No suitable Chrome/Edge binary found. "
                "Please start Chrome manually with: chrome.exe "
                f"--remote-debugging-port={cdp_port(browser_cdp_url)} "
                f'--user-data-dir="{profile_dir}"'
            )
        raise RuntimeError(
            f"No Chrome instance found on {browser_cdp_url}. "
            "Please start Chrome with --remote-debugging-port or set launch_if_missing=true."
        )
    valid_modes = {"attached", "headless", "headed"}
    is_headless = browser_mode == "headless" or browser_mode not in valid_modes
    browser = await playwright.chromium.launch(headless=is_headless)
    context = await browser.new_context(accept_downloads=accept_downloads)
    page = await context.new_page()
    return {
        "browser": browser,
        "context": context,
        "page": page,
        "attached": False,
        "created_context": True,
        "close_browser": True,
    }


async def close_browser_session(session: dict[str, Any]) -> None:
    page = session.get("page")
    context = session.get("context")
    browser = session.get("browser")

    async def _close_step(awaitable: Any) -> None:
        try:
            await awaitable
        except Exception:
            return

    if page is not None:
        await _close_step(page.close())
    if session.get("attached"):
        if session.get("created_context") and context is not None:
            await _close_step(context.close())
        disconnect = getattr(browser, "disconnect", None)
        if callable(disconnect):
            await _close_step(disconnect())
        return
    if context is not None:
        await _close_step(context.close())
    if browser is not None:
        await _close_step(browser.close())


__all__ = [
    "attachable_browser_candidates",
    "cdp_port",
    "close_browser_session",
    "connect_attached_browser",
    "launch_attachable_browser",
    "open_browser_session",
]
