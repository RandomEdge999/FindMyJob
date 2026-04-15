"""Captcha detection and solving service.

Supports 2Captcha-compatible APIs (2Captcha, CapMonster, Anti-Captcha).
Configurable strategy: solve, skip, or manual (pause for human).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger("findmyjob.captcha")


class CaptchaSolverError(Exception):
    """Raised when captcha solving fails after all attempts."""


class CaptchaSolver:
    """Integrates with 2Captcha-compatible APIs to solve captchas automatically."""

    SUPPORTED_PROVIDERS = ("2captcha", "capmonster", "anti-captcha")

    # Polling settings
    POLL_INTERVAL = 5.0  # seconds between result checks
    MAX_POLL_ATTEMPTS = 60  # 5 min max wait

    def __init__(
        self,
        api_key: str,
        provider: str = "2captcha",
        timeout: float = 300.0,
    ) -> None:
        self.api_key = api_key
        self.provider = provider.lower().strip()
        self.timeout = timeout

        if self.provider == "2captcha":
            self.base_url = "https://2captcha.com"
        elif self.provider == "capmonster":
            self.base_url = "https://api.capmonster.cloud"
        elif self.provider == "anti-captcha":
            self.base_url = "https://api.anti-captcha.com"
        else:
            raise ValueError(f"Unsupported captcha provider: {provider}. Use one of: {self.SUPPORTED_PROVIDERS}")

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> str:
        """Solve a reCAPTCHA v2 challenge and return the response token."""
        log.info("[captcha] Submitting reCAPTCHA v2 task (provider=%s, url=%s)", self.provider, page_url)

        if self.provider == "2captcha":
            return await self._solve_2captcha_recaptcha_v2(site_key, page_url)
        else:
            return await self._solve_generic_recaptcha_v2(site_key, page_url)

    async def solve_hcaptcha(self, site_key: str, page_url: str) -> str:
        """Solve an hCaptcha challenge and return the response token."""
        log.info("[captcha] Submitting hCaptcha task (provider=%s, url=%s)", self.provider, page_url)

        if self.provider == "2captcha":
            return await self._solve_2captcha_hcaptcha(site_key, page_url)
        else:
            return await self._solve_generic_hcaptcha(site_key, page_url)

    # --- 2Captcha-style API (also works with CapMonster's 2captcha compat endpoint) ---

    async def _solve_2captcha_recaptcha_v2(self, site_key: str, page_url: str) -> str:
        params = {
            "key": self.api_key,
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
            "json": "1",
        }
        return await self._2captcha_submit_and_poll(params)

    async def _solve_2captcha_hcaptcha(self, site_key: str, page_url: str) -> str:
        params = {
            "key": self.api_key,
            "method": "hcaptcha",
            "sitekey": site_key,
            "pageurl": page_url,
            "json": "1",
        }
        return await self._2captcha_submit_and_poll(params)

    async def _2captcha_submit_and_poll(self, params: dict[str, str]) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Submit task
            response = await client.get(f"{self.base_url}/in.php", params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != 1:
                error = data.get("request", "unknown_error")
                raise CaptchaSolverError(f"2Captcha submit failed: {error}")

            task_id = data["request"]
            log.info("[captcha] Task submitted: id=%s", task_id)

            # Poll for result
            result_params = {"key": self.api_key, "action": "get", "id": task_id, "json": "1"}
            t0 = time.monotonic()
            for attempt in range(self.MAX_POLL_ATTEMPTS):
                if time.monotonic() - t0 > self.timeout:
                    raise CaptchaSolverError(f"Captcha solve timed out after {self.timeout}s")

                await asyncio.sleep(self.POLL_INTERVAL)
                result = await client.get(f"{self.base_url}/res.php", params=result_params)
                result.raise_for_status()
                result_data = result.json()

                if result_data.get("status") == 1:
                    token = result_data["request"]
                    elapsed = time.monotonic() - t0
                    log.info("[captcha] Solved in %.1fs (attempts=%d)", elapsed, attempt + 1)
                    return token

                if result_data.get("request") == "CAPCHA_NOT_READY":
                    continue

                error = result_data.get("request", "unknown_error")
                raise CaptchaSolverError(f"2Captcha solve failed: {error}")

            raise CaptchaSolverError("Captcha solve exceeded max poll attempts")

    # --- CapMonster / Anti-Captcha native API ---

    async def _solve_generic_recaptcha_v2(self, site_key: str, page_url: str) -> str:
        task = {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        return await self._generic_submit_and_poll(task)

    async def _solve_generic_hcaptcha(self, site_key: str, page_url: str) -> str:
        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        return await self._generic_submit_and_poll(task)

    async def _generic_submit_and_poll(self, task: dict[str, str]) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Submit
            payload = {"clientKey": self.api_key, "task": task}
            response = await client.post(f"{self.base_url}/createTask", json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errorId", 0) != 0:
                raise CaptchaSolverError(f"Captcha submit failed: {data.get('errorDescription', 'unknown')}")

            task_id = data["taskId"]
            log.info("[captcha] Task submitted: id=%s", task_id)

            # Poll
            t0 = time.monotonic()
            for attempt in range(self.MAX_POLL_ATTEMPTS):
                if time.monotonic() - t0 > self.timeout:
                    raise CaptchaSolverError(f"Captcha solve timed out after {self.timeout}s")

                await asyncio.sleep(self.POLL_INTERVAL)
                result = await client.post(
                    f"{self.base_url}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                result.raise_for_status()
                result_data = result.json()

                if result_data.get("errorId", 0) != 0:
                    raise CaptchaSolverError(f"Captcha solve failed: {result_data.get('errorDescription', 'unknown')}")

                if result_data.get("status") == "ready":
                    solution = result_data.get("solution", {})
                    token = solution.get("gRecaptchaResponse") or solution.get("token") or ""
                    elapsed = time.monotonic() - t0
                    log.info("[captcha] Solved in %.1fs (attempts=%d)", elapsed, attempt + 1)
                    return token

            raise CaptchaSolverError("Captcha solve exceeded max poll attempts")


def extract_recaptcha_site_key(dom_html: str) -> str | None:
    """Extract reCAPTCHA site key from DOM HTML."""
    patterns = [
        r'data-sitekey="([^"]+)"',
        r"data-sitekey='([^']+)'",
        r'render=([A-Za-z0-9_-]{20,})',
        r'"sitekey"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, dom_html)
        if match:
            return match.group(1)
    return None


def extract_hcaptcha_site_key(dom_html: str) -> str | None:
    """Extract hCaptcha site key from DOM HTML."""
    patterns = [
        r'data-sitekey="([^"]+)"',
        r"data-sitekey='([^']+)'",
        r'"sitekey"\s*:\s*"([^"]+)"',
    ]
    lowered = dom_html.lower()
    if "hcaptcha" not in lowered:
        return None
    for pattern in patterns:
        match = re.search(pattern, dom_html)
        if match:
            return match.group(1)
    return None


def detect_captcha_type(dom_html: str) -> str | None:
    """Detect which type of captcha is present: 'recaptcha', 'hcaptcha', or None."""
    lowered = dom_html.lower()
    if "hcaptcha" in lowered or "h-captcha" in lowered:
        return "hcaptcha"
    if "recaptcha" in lowered or "grecaptcha" in lowered:
        return "recaptcha"
    if "captcha" in lowered:
        return "recaptcha"  # default assumption
    return None
