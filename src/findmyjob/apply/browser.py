from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio

from findmyjob.apply.browser_session import (
    attachable_browser_candidates as session_attachable_browser_candidates,
    cdp_port as session_cdp_port,
    close_browser_session as session_close_browser_session,
    connect_attached_browser as session_connect_attached_browser,
    launch_attachable_browser as session_launch_attachable_browser,
    open_browser_session as session_open_browser_session,
)
from findmyjob.core.enums import CaptureMode, JobLifecycleStatus

log = logging.getLogger("findmyjob.browser")
from findmyjob.apply.captcha import CaptchaSolver, CaptchaSolverError, detect_captcha_type, extract_hcaptcha_site_key, extract_recaptcha_site_key
from findmyjob.core.email_otp import fetch_greenhouse_security_code
from findmyjob.core.types import FormFieldBinding, SubmissionCapturePolicy, SubmissionEvidence, SubmissionPlan, SubmissionResult

_LOGIN_WALL_PATTERNS = ("sign in", "log in", "create an account", "sign_in", "/login")
_CAPTCHA_PATTERNS = ("captcha", "hcaptcha", "recaptcha", "anti-bot")

_ASHBY_SUBMIT_SELECTORS = (
    'button[data-testid="submit-application"]',
    'button:has-text("Submit Application")',
    'button:has-text("Submit application")',
    '.ashby-application-form-submit-button',
)
_GENERIC_SUBMIT_SELECTOR = ", ".join((
    "#submit_app",
    "button[type='submit']",
    "input[type='submit']",
    *_ASHBY_SUBMIT_SELECTORS,
))
_GREENHOUSE_SUBMIT_SELECTOR = _GENERIC_SUBMIT_SELECTOR
_GREENHOUSE_SUCCESS_TEXT_PATTERNS = [
    "text=/thank you/i",
    "text=/application received/i",
    "text=/application submitted/i",
    "text=/your application has been submitted/i",
]
_GREENHOUSE_CONFIRMATION_SELECTORS = [
    ".application_confirmation",
    ".application-confirmation",
    ".thank-you",
    ".thank_you",
    ".success",
    "[data-qa='application-confirmation']",
    "[data-testid='application-confirmation']",
]
_GREENHOUSE_SUCCESS_URL_MARKERS = ("thank_you", "thank-you", "submitted", "application_submitted")
_GREENHOUSE_EMAIL_VERIFICATION_PATTERNS = (
    "verification code was sent",
    "security code",
    "confirm you're a human",
)
_DEMOGRAPHIC_FALLBACK_TOKENS = (
    "decline",
    "prefer not",
    "don't wish",
    "do not wish",
    "wish not",
    "not to say",
    "not answer",
    "choose not",
    "self-identify",
)


async def _sleep_ms(delay_ms: int | float) -> None:
    """Use a page-independent pause so navigations/closures don't explode mid-wait."""
    await anyio.sleep(max(float(delay_ms), 0.0) / 1000.0)


def _is_demographic_binding(binding: FormFieldBinding) -> bool:
    section = str(binding.metadata.get("section") or binding.metadata.get("group") or "").strip().casefold()
    prompt = f"{binding.prompt_text} {binding.source_field_name}".casefold()
    return any(token in section for token in ("demographic", "eeoc", "diversity")) or any(token in prompt for token in ("demographic", "eeoc", "veteran", "disability", "gender", "race", "ethnicity"))


def _preferred_decline_option(option_details: list[dict[str, Any]]) -> dict[str, str] | None:
    for option in option_details:
        label = str(option.get("label") or "").strip()
        value = str(option.get("value") or option.get("id") or label).strip()
        lowered = f"{label} {value}".casefold()
        if any(token in lowered for token in _DEMOGRAPHIC_FALLBACK_TOKENS):
            return {"label": label or value, "value": value or label}
    return None


def _normalize_date_candidate(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except Exception:
        pass
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    if match:
        month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    match = re.search(r"\b([A-Za-z]+)\s+(\d{4})\b", text)
    if match:
        month_lookup = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        month = month_lookup.get(match.group(1).strip().casefold())
        if month is not None:
            try:
                return date(int(match.group(2)), month, 1).isoformat()
            except ValueError:
                return None
    return None


def _merge_field_audit_entry(field_audit: list[dict[str, Any]], audit: dict[str, Any], *, second_pass: bool = False) -> None:
    if second_pass:
        for existing in reversed(field_audit):
            if existing.get("field") == audit.get("field") and existing.get("prompt") == audit.get("prompt"):
                existing.update(audit)
                existing["second_pass"] = True
                return
    field_audit.append(audit)


async def _locator_descriptor(locator: Any) -> dict[str, Any]:
    try:
        return await locator.evaluate(
            """
            (el) => {
              const compact = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const labels = el.labels ? Array.from(el.labels).map((node) => compact(node.textContent)).filter(Boolean) : [];
              const wrapper = el.closest('fieldset, .application-question, .field, .question, .application-field, li, div');
              const heading = wrapper ? wrapper.querySelector('label, legend, .application-label, .field-label, strong, h1, h2, h3') : null;
              return {
                tag: el.tagName.toLowerCase(),
                type: compact(el.getAttribute('type')).toLowerCase(),
                name: compact(el.getAttribute('name')),
                id: compact(el.id),
                label: labels[0] || compact(el.getAttribute('aria-label')) || compact(heading ? heading.textContent : '') || compact(el.getAttribute('placeholder')) || compact(el.getAttribute('name')) || compact(el.id),
                placeholder: compact(el.getAttribute('placeholder')),
                role: compact(el.getAttribute('role')).toLowerCase(),
                aria_autocomplete: compact(el.getAttribute('aria-autocomplete')).toLowerCase(),
                value: compact(el.value),
                checked: !!el.checked,
              };
            }
            """
        )
    except Exception:
        return {}


def _descriptor_matches_binding(descriptor: dict[str, Any], binding: FormFieldBinding) -> bool:
    descriptor_text = " ".join(
        part
        for part in (
            str(descriptor.get("name") or "").strip(),
            str(descriptor.get("id") or "").strip(),
            str(descriptor.get("label") or "").strip(),
        )
        if part
    ).casefold()
    for candidate in (binding.source_field_name, binding.prompt_text):
        normalized = str(candidate or "").strip().casefold()
        if not normalized:
            continue
        if normalized == descriptor_text or normalized in descriptor_text or descriptor_text in normalized:
            return True
    return False


def _descriptor_has_value(descriptor: dict[str, Any]) -> bool:
    widget_type = str(descriptor.get("type") or descriptor.get("tag") or "").strip().casefold()
    if widget_type in {"checkbox", "radio"}:
        return bool(descriptor.get("checked"))
    return bool(str(descriptor.get("value") or "").strip())


class GreenhouseBindingError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(slots=True)
class GreenhouseArtifactPaths:
    pre_submit_snapshot_path: Path
    final_snapshot_path: Path
    trace_path: Path
    pre_submit_dom_path: Path
    post_submit_dom_path: Path


class GreenhouseHostedFormFlow:
    def __init__(self, page: Any, plan: SubmissionPlan, artifacts: GreenhouseArtifactPaths, capture_policy: SubmissionCapturePolicy) -> None:
        self.page = page
        self.plan = plan
        self.artifacts = artifacts
        self.capture_policy = capture_policy
        self.field_audit: list[dict[str, Any]] = []

    async def _prepare_form(self, url: str) -> tuple[str, dict[str, Any], str | None, str]:
        log.info("    [form] Navigating to: %s", url)
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            log.warning("    [form] Navigation to %s failed: %s. Retrying with longer wait...", url[:80], exc)
            try:
                await self.page.goto(url, wait_until="commit", timeout=90000)
            except Exception as retry_exc:
                log.error("    [form] Navigation retry also failed: %s", retry_exc)
                return url, {}, "navigation_failed", f"Could not load page: {retry_exc}"
        try:
            await self.page.wait_for_load_state("networkidle")
        except Exception:
            log.info("    [form] networkidle timeout, continuing after 1500ms fallback")
            await _sleep_ms(1500)

        initial_url = getattr(self.page, "url", url)
        log.info("    [form] Page loaded (url=%s). Filling %d form fields...", initial_url[:80], len(self.plan.fields))
        for i, binding in enumerate(self.plan.fields, 1):
            log.info(
                "    [form]   Field %d/%d: %s (%s)",
                i,
                len(self.plan.fields),
                binding.source_field_name or "unnamed",
                binding.widget_type or "?",
            )
            try:
                _merge_field_audit_entry(self.field_audit, await self._apply_binding(binding))
            except Exception as exc:
                log.warning("    [form] Unhandled binding failure for field %s: %s", binding.source_field_name, exc)
                _merge_field_audit_entry(
                    self.field_audit,
                    {
                        "field": binding.source_field_name,
                        "prompt": binding.prompt_text,
                        "widget_type": binding.widget_type,
                        "required": binding.required,
                        "status": "error",
                        "value_summary": self._value_summary(binding),
                        "error_text": str(exc),
                        "failure_reason": "submit_exception",
                        "binding_strategy": None,
                        "section": binding.metadata.get("section"),
                    },
                )

        await self._second_pass_visible_required_fields()

        pre_state = await self._collect_form_state()
        if self.capture_policy.dom_snapshots != CaptureMode.OFF:
            try:
                await self._write_dom_snapshot(self.artifacts.pre_submit_dom_path)
            except Exception as exc:
                log.warning("    [form] DOM snapshot failed: %s", exc)
        if self.capture_policy.screenshots != CaptureMode.OFF:
            try:
                await self.page.screenshot(path=str(self.artifacts.pre_submit_snapshot_path), full_page=True)
            except Exception as exc:
                log.warning("    [form] Pre-submit screenshot failed: %s", exc)
        failure_reason, message = self._classify_pre_submit_failure(pre_state)
        return initial_url, pre_state, failure_reason, message

    async def _second_pass_visible_required_fields(self) -> None:
        await _sleep_ms(500)
        selectors = "input:visible[required], select:visible[required], textarea:visible[required]"
        try:
            locators = await self.page.locator(selectors).all()
        except Exception as exc:
            log.debug("    [form] Second-pass locator collection failed: %s", exc)
            return
        for locator in locators:
            try:
                descriptor = await _locator_descriptor(locator)
                if not descriptor or _descriptor_has_value(descriptor):
                    continue
                binding = self._binding_for_descriptor(descriptor)
                if binding is None:
                    continue
                _merge_field_audit_entry(self.field_audit, await self._apply_binding(binding), second_pass=True)
            except Exception as exc:
                log.debug("    [form] Second-pass field retry failed: %s", exc)
        await self._second_pass_eeoc_required_fields()

    async def _second_pass_eeoc_required_fields(self) -> None:
        await _sleep_ms(250)
        selectors = ", ".join(
            (
                ".eeoc__question__wrapper input[role='combobox'][aria-required='true']",
                ".eeoc__container input[role='combobox'][aria-required='true']",
            )
        )
        try:
            locators = await self.page.locator(selectors).all()
        except Exception as exc:
            log.debug("    [form] EEO second-pass locator collection failed: %s", exc)
            return
        for locator in locators:
            try:
                descriptor = await _locator_descriptor(locator)
                if not descriptor:
                    continue
                binding = self._binding_for_descriptor(descriptor)
                if binding is not None:
                    audit = await self._apply_binding(binding)
                    _merge_field_audit_entry(self.field_audit, audit, second_pass=True)
                    if audit.get("status") == "bound":
                        continue
                if await self._combobox_has_selection(locator) and not await self._combobox_is_invalid(locator):
                    continue
                selected_label = await self._select_decline_eeoc_option(locator)
                if not selected_label:
                    continue
                prompt = str(descriptor.get("label") or descriptor.get("name") or "EEOC question").strip() or "EEOC question"
                _merge_field_audit_entry(
                    self.field_audit,
                    {
                        "field": str(descriptor.get("name") or descriptor.get("id") or prompt).strip(),
                        "prompt": prompt,
                        "widget_type": "select",
                        "required": True,
                        "status": "bound",
                        "value_summary": selected_label,
                        "error_text": None,
                        "failure_reason": None,
                        "binding_strategy": "eeoc_decline_fallback",
                        "section": "eeoc",
                    },
                    second_pass=True,
                )
            except Exception as exc:
                log.debug("    [form] EEO second-pass retry failed: %s", exc)

    def _binding_for_descriptor(self, descriptor: dict[str, Any]) -> FormFieldBinding | None:
        for binding in self.plan.fields:
            if _descriptor_matches_binding(descriptor, binding):
                return binding
        return None

    async def _combobox_has_selection(self, locator: Any) -> bool:
        try:
            return bool(
                await locator.evaluate(
                    """
                    (el) => {
                      const wrapper = el.closest('.select-shell, .select__container, .select, .phone-input__country');
                      if (!wrapper) return false;
                      if (wrapper.querySelector('.select__single-value, .select__multi-value')) return true;
                      const valueContainer = wrapper.querySelector('.select__value-container');
                      return !!(valueContainer && /has-value/.test(String(valueContainer.className || '').toLowerCase()));
                    }
                    """
                )
            )
        except Exception:
            return False

    async def _combobox_is_invalid(self, locator: Any) -> bool:
        try:
            return str(await locator.get_attribute("aria-invalid") or "").strip().lower() == "true"
        except Exception:
            return False

    async def _finalize_combobox_selection(self, locator: Any) -> bool:
        try:
            await locator.evaluate("(el) => { if (el && typeof el.blur === 'function') el.blur(); }")
        except Exception:
            try:
                await locator.press("Tab")
            except Exception:
                pass
        await _sleep_ms(150)
        return await self._combobox_has_selection(locator)

    async def _clear_combobox_selection(self, locator: Any) -> None:
        try:
            await locator.evaluate(
                """
                (el) => {
                  const wrapper = el.closest('.select-shell, .select__container, .select, .phone-input__country');
                  const clearButton = wrapper ? wrapper.querySelector("button[aria-label*='Clear']") : null;
                  if (clearButton) clearButton.click();
                }
                """
            )
        except Exception:
            return
        await _sleep_ms(100)

    async def _select_decline_eeoc_option(self, locator: Any) -> str | None:
        candidates = [
            "Decline To Self Identify",
            "I don't wish to answer",
            "I do not want to answer",
            "I don't want to answer",
            "Prefer not to answer",
            "Choose not to self identify",
        ]
        try:
            await locator.click()
        except Exception as exc:
            log.debug("    [form] EEO combobox click failed: %s: %s", type(exc).__name__, exc)
        for candidate in candidates:
            if await self._combobox_has_selection(locator) and await self._combobox_is_invalid(locator):
                await self._clear_combobox_selection(locator)
            try:
                await locator.fill(candidate)
            except Exception:
                try:
                    await locator.press("Control+A")
                    await locator.press("Backspace")
                    await locator.type(candidate)
                except Exception as exc:
                    log.debug("    [form] EEO combobox type fallback failed for '%s': %s", candidate, exc)
                    continue
            await _sleep_ms(200)
            if await self._click_combobox_option(locator, candidate):
                await self._finalize_combobox_selection(locator)
                if await self._combobox_has_selection(locator):
                    return candidate
            try:
                await locator.press("ArrowDown")
                await _sleep_ms(100)
                await locator.press("Enter")
                await self._finalize_combobox_selection(locator)
                if await self._combobox_has_selection(locator):
                    return candidate
            except Exception as exc:
                log.debug("    [form] EEO combobox ArrowDown+Enter failed for '%s': %s", candidate, exc)
        return None

    async def preview(self, url: str) -> SubmissionResult:
        _initial_url, pre_state, failure_reason, message = await self._prepare_form(url)
        if failure_reason:
            log.warning("    [form] Preview failed before submit: %s - %s", failure_reason, message)
            return self._result(
                submitted=False,
                message=message,
                failure_reason=failure_reason,
                pre_state=pre_state,
                post_state=None,
                confirmation_text=None,
                confirmation_strategy=None,
                matched_confirmation_markers=[],
            )

        submit_button = self.page.locator(_GREENHOUSE_SUBMIT_SELECTOR)
        button_count = await submit_button.count()
        log.info("    [form] Preview located %d submit button(s)", button_count)
        if button_count == 0:
            pre_state = {**pre_state, "submit_button_present": False, "submit_button_enabled": None}
            return self._result(
                submitted=False,
                message="Greenhouse submit button was not found",
                failure_reason="submit_button_missing",
                pre_state=pre_state,
                post_state=None,
                confirmation_text=None,
                confirmation_strategy=None,
                matched_confirmation_markers=[],
            )

        if self.capture_policy.screenshots != CaptureMode.OFF:
            await self.page.screenshot(path=str(self.artifacts.final_snapshot_path), full_page=True)
        if self.capture_policy.dom_snapshots != CaptureMode.OFF:
            await self._write_dom_snapshot(self.artifacts.post_submit_dom_path)
        post_state = await self._collect_form_state()
        return self._result(
            submitted=False,
            message="Preview ready; submit not clicked",
            failure_reason=None,
            pre_state=pre_state,
            post_state=post_state,
            confirmation_text=None,
            confirmation_strategy="pre_submit_preview",
            matched_confirmation_markers=[],
            status=JobLifecycleStatus.READY_FOR_REVIEW,
        )

    async def submit(self, url: str) -> SubmissionResult:
        initial_url, pre_state, failure_reason, message = await self._prepare_form(url)
        if failure_reason and failure_reason != "pre_submit_validation_failed":
            log.warning("    [form] Pre-submit failure: %s - %s", failure_reason, message)
            return self._result(
                submitted=False,
                message=message,
                failure_reason=failure_reason,
                pre_state=pre_state,
                post_state=None,
                confirmation_text=None,
                confirmation_strategy=None,
                matched_confirmation_markers=[],
            )
        if failure_reason == "pre_submit_validation_failed":
            log.info("    [form] Proceeding despite pre-submit validation markers; submit click will determine final outcome")

        submit_button = self.page.locator(_GREENHOUSE_SUBMIT_SELECTOR)
        button_count = await submit_button.count()
        log.info("    [form] Found %d submit button(s)", button_count)
        if button_count == 0:
            pre_state = {**pre_state, "submit_button_present": False, "submit_button_enabled": None}
            return self._result(
                submitted=False,
                message="Greenhouse submit button was not found",
                failure_reason="submit_button_missing",
                pre_state=pre_state,
                post_state=None,
                confirmation_text=None,
                confirmation_strategy=None,
                matched_confirmation_markers=[],
            )

        log.info("    [form] CLICKING SUBMIT BUTTON...")
        await submit_button.first.click()
        log.info("    [form] Submit clicked, waiting for page response...")
        await _sleep_ms(1500)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            await _sleep_ms(1000)

        if self.capture_policy.screenshots != CaptureMode.OFF:
            await self.page.screenshot(path=str(self.artifacts.final_snapshot_path), full_page=True)
        if self.capture_policy.dom_snapshots != CaptureMode.OFF:
            await self._write_dom_snapshot(self.artifacts.post_submit_dom_path)
        post_state = await self._collect_form_state()
        classification = await self._classify_post_submit_result(initial_url, post_state)
        if classification["failure_reason"] == "email_verification_required":
            verification_code = self._manual_email_verification_code()
            if not verification_code:
                try:
                    issued_after = datetime.now(timezone.utc) - timedelta(minutes=2)
                    verification_code = fetch_greenhouse_security_code(
                        recipient=classification.get("confirmation_text"),
                        issued_after=issued_after,
                    )
                except Exception as exc:
                    log.warning("    [form] Email OTP fetch failed: %s: %s", type(exc).__name__, exc)
            if verification_code:
                classification = await self._retry_submit_with_email_verification_code(
                    initial_url=initial_url,
                    pre_state=pre_state,
                    code=verification_code,
                )
        log.info(
            "    [form] Post-submit classification: submitted=%s, message=%s, failure=%s",
            classification["submitted"],
            (classification["message"] or "")[:80],
            classification["failure_reason"],
        )
        return self._result(
            submitted=classification["submitted"],
            message=classification["message"],
            failure_reason=classification["failure_reason"],
            pre_state=pre_state,
            post_state=post_state,
            confirmation_text=classification["confirmation_text"],
            confirmation_strategy=classification["confirmation_strategy"],
            matched_confirmation_markers=classification["matched_confirmation_markers"],
        )

    async def _apply_binding(self, binding: FormFieldBinding) -> dict[str, Any]:
        audit = {
            "field": binding.source_field_name,
            "prompt": binding.prompt_text,
            "widget_type": binding.widget_type,
            "required": binding.required,
            "status": "not_applicable",
            "value_summary": self._value_summary(binding),
            "error_text": None,
            "failure_reason": None,
            "binding_strategy": None,
            "section": binding.metadata.get("section"),
        }
        try:
            if binding.artifact_binding is not None:
                bound, strategy = await self._set_greenhouse_file(binding)
            elif self._is_date_binding(binding):
                bound, strategy = await self._fill_greenhouse_date(binding)
            elif binding.widget_type in {"select", "dropdown", "checkbox", "checkbox_group", "radio", "radio_group"} or binding.metadata.get("option_details"):
                bound, strategy = await self._bind_greenhouse_choice(binding)
            elif self._can_skip_artifact_companion(binding):
                audit["binding_strategy"] = "artifact_companion_optional"
                audit["status"] = "not_applicable"
                return audit
            else:
                bound, strategy = await self._fill_greenhouse_text(binding)

            audit["binding_strategy"] = strategy
            if bound:
                audit["status"] = "bound"
            elif binding.required:
                audit["status"] = "missing"
            else:
                audit["status"] = "not_applicable"
        except GreenhouseBindingError as exc:
            audit["status"] = "error"
            audit["failure_reason"] = exc.reason
            audit["error_text"] = str(exc)
        except Exception as exc:
            log.warning("    [form] Binding error for field %s: %s: %s", binding.source_field_name, type(exc).__name__, exc)
            audit["status"] = "error"
            audit["failure_reason"] = "submit_exception"
            audit["error_text"] = str(exc)
            audit["error_type"] = type(exc).__name__
        return audit

    @staticmethod
    def _is_date_binding(binding: FormFieldBinding) -> bool:
        widget_type = str(binding.widget_type or "").strip().casefold()
        field_type = str(binding.metadata.get("type") or binding.metadata.get("input_type") or "").strip().casefold()
        return widget_type in {"date", "input_date"} or field_type == "date"

    async def _fill_greenhouse_date(self, binding: FormFieldBinding) -> tuple[bool, str | None]:
        locator, strategy = await self._greenhouse_locator(binding)
        if locator is None:
            return False, None
        raw_values = [binding.value, *binding.values]
        normalized = next((candidate for candidate in (_normalize_date_candidate(value) for value in raw_values) if candidate), None)
        if normalized is None:
            prompt = f"{binding.prompt_text} {binding.source_field_name}".casefold()
            if any(token in prompt for token in ("start date", "availability", "earliest", "when can you", "graduation")):
                normalized = (date.today() + timedelta(days=14)).isoformat()
        if normalized is None:
            return False, strategy
        await locator.fill(normalized)
        return True, f"{strategy}:date"

    def _is_artifact_companion_text(self, binding: FormFieldBinding) -> bool:
        lowered = f"{binding.prompt_text} {binding.source_field_name}".lower()
        return binding.widget_type == 'textarea' and any(token in lowered for token in ('resume', 'cover'))

    def _can_skip_artifact_companion(self, binding: FormFieldBinding) -> bool:
        if not self._is_artifact_companion_text(binding):
            return False
        prompt = str(binding.prompt_text or '').strip().casefold()
        for audit in self.field_audit:
            if audit.get('status') != 'bound' or str(audit.get('widget_type') or '').lower() != 'file':
                continue
            other_prompt = str(audit.get('prompt') or '').strip().casefold()
            if prompt and other_prompt and (prompt == other_prompt or prompt in other_prompt or other_prompt in prompt):
                return True
        return False

    async def _fill_greenhouse_text(self, binding: FormFieldBinding) -> tuple[bool, str | None]:
        locator, strategy = await self._greenhouse_locator(binding)
        if locator is None:
            return False, None
        value = binding.value or (binding.values[0] if binding.values else "")
        if not value:
            return False, strategy
        if self._is_location_field(binding):
            search_value = self._preferred_location_search_value(value)
            await locator.fill(search_value)
            await self._complete_location_autocomplete(binding, locator, search_value)
            return True, f"{strategy}:location"
        await locator.fill(value)
        if await self._locator_has_autocomplete(locator):
            if await self._select_combobox_choice(binding, locator, self._dedupe_strings([value])):
                return True, f"{strategy}:combobox"
        return True, strategy

    async def _bind_greenhouse_choice(self, binding: FormFieldBinding) -> tuple[bool, str | None]:
        choices = self._choice_candidates(binding)
        if not choices:
            return False, None

        if binding.widget_type in {"checkbox", "checkbox_group"}:
            if await self._check_choice_inputs(binding, choices, "checkbox"):
                return True, "checkbox_group"

        if binding.widget_type in {"radio", "radio_group"}:
            if await self._check_choice_inputs(binding, choices[:1], "radio"):
                return True, "radio_group"

        if await self._select_choice(binding, choices):
            return True, "select_option"
        if await self._check_choice_inputs(binding, choices[:1], "radio"):
            return True, "radio_group"
        if await self._check_choice_inputs(binding, choices, "checkbox"):
            return True, "checkbox_group"
        return False, None

    async def _select_choice(self, binding: FormFieldBinding, choices: list[dict[str, str]]) -> bool:
        locator, _strategy = await self._greenhouse_locator(binding)
        if locator is None:
            return False
        values = [choice["value"] for choice in choices if choice["value"]]
        labels = [choice["label"] for choice in choices if choice["label"]]
        # Check if this is a combobox (React-style) to avoid misusing select_option
        raw_type = str((binding.metadata.get('submission_binding') or {}).get('raw_type') or '').strip().lower()
        role = (await self._safe_get_attribute(locator, 'role') or '').strip().lower()
        aria_autocomplete = (await self._safe_get_attribute(locator, 'aria-autocomplete') or '').strip().lower()
        if raw_type == 'multi_value_single_select' or role == 'combobox' or aria_autocomplete == 'list':
            return await self._select_combobox_choice(binding, locator, self._dedupe_strings([*labels, *values]))
        if await self._select_native_option(locator, values, labels):
            return True
        return await self._select_combobox_choice(binding, locator, self._dedupe_strings([*labels, *values]))

    async def _select_native_option(self, locator: Any, values: list[str], labels: list[str]) -> bool:
        if values:
            try:
                payload: str | list[str] = values[0] if len(values) == 1 else values
                await locator.select_option(value=payload)
                return True
            except Exception as exc:
                log.debug("    [form] select_option by value failed: %s: %s", type(exc).__name__, exc)
        if labels:
            try:
                payload: str | list[str] = labels[0] if len(labels) == 1 else labels
                await locator.select_option(label=payload)
                return True
            except Exception as exc:
                log.debug("    [form] select_option by label failed: %s: %s", type(exc).__name__, exc)
        return False

    async def _select_combobox_choice(self, binding: FormFieldBinding, locator: Any, candidates: list[str]) -> bool:
        raw_type = str((binding.metadata.get('submission_binding') or {}).get('raw_type') or '').strip().lower()
        role = (await self._safe_get_attribute(locator, 'role') or '').strip().lower()
        aria_autocomplete = (await self._safe_get_attribute(locator, 'aria-autocomplete') or '').strip().lower()
        if raw_type != 'multi_value_single_select' and role != 'combobox' and aria_autocomplete != 'list':
            return False
        try:
            await locator.click()
        except Exception as exc:
            log.debug("    [form] combobox click failed for %s: %s: %s", binding.source_field_name, type(exc).__name__, exc)
        for candidate in self._dedupe_strings(candidates):
            try:
                await locator.fill(candidate)
            except Exception as fill_exc:
                log.debug("    [form] combobox fill failed for %s, trying type fallback: %s", binding.source_field_name, fill_exc)
                try:
                    await locator.press('Control+A')
                    await locator.press('Backspace')
                    await locator.type(candidate)
                except Exception as type_exc:
                    log.warning("    [form] combobox type fallback also failed for %s: %s: %s", binding.source_field_name, type(type_exc).__name__, type_exc)
            await _sleep_ms(200)
            if await self._click_combobox_option(locator, candidate):
                return await self._finalize_combobox_selection(locator)
            try:
                await locator.press('ArrowDown')
                await _sleep_ms(100)
                await locator.press('Enter')
                if await self._finalize_combobox_selection(locator):
                    return True
            except Exception as exc:
                log.debug("    [form] ArrowDown+Enter fallback failed for %s with candidate '%s': %s", binding.source_field_name, candidate, exc)
                continue
        return False

    async def _click_combobox_option(self, locator: Any, candidate: str) -> bool:
        normalized = self._normalize_choice(candidate)
        if not normalized:
            return False
        listbox_id = ((await self._safe_get_attribute(locator, 'aria-controls')) or (await self._safe_get_attribute(locator, 'aria-owns')) or '').strip()
        option_groups: list[Any] = []
        if listbox_id:
            option_groups.append(self.page.locator(f"[id={self._css_string(listbox_id)}] [role='option']"))
            option_groups.append(self.page.locator(f"[id={self._css_string(listbox_id)}] *[role='option']"))
        option_groups.append(self.page.locator("[role='listbox'] [role='option']"))
        option_groups.append(self.page.locator(".select__menu [role='option']"))
        option_groups.append(self.page.locator("[role='option']"))

        exact_match = None
        loose_match = None
        for options in option_groups:
            try:
                count = min(await options.count(), 50)
            except Exception:
                continue
            for index in range(count):
                option = options.nth(index)
                try:
                    if not await option.is_visible():
                        continue
                    option_text = self._normalize_choice(await option.inner_text())
                except Exception:
                    continue
                if not option_text:
                    continue
                if option_text == normalized:
                    exact_match = option
                    break
                if loose_match is None and (option_text.startswith(normalized) or normalized.startswith(option_text)):
                    loose_match = option
            if exact_match is not None:
                break

        target = exact_match or loose_match
        if target is None:
            return False
        try:
            await target.click()
        except Exception:
            return False
        return True

    async def _check_choice_inputs(self, binding: FormFieldBinding, choices: list[dict[str, str]], input_type: str) -> bool:
        if not choices:
            return False
        matched = 0
        for choice in choices:
            locator = await self._choice_input_locator(binding, choice, input_type)
            if locator is None:
                return False
            await locator.check()
            matched += 1
        return matched == len(choices)

    async def _choice_input_locator(self, binding: FormFieldBinding, choice: dict[str, str], input_type: str):
        for name in self._name_candidates(binding):
            for candidate in self._dedupe_strings([choice["value"], choice["label"]]):
                locator = self.page.locator(
                    f"input[type='{input_type}'][name={self._css_string(name)}][value={self._css_string(candidate)}]"
                )
                if await locator.count() > 0:
                    return locator.first
        for candidate in self._dedupe_strings([choice["value"], choice["label"]]):
            locator = self.page.locator(f"input[type='{input_type}'][value={self._css_string(candidate)}]")
            if await locator.count() > 0:
                return locator.first
        label = choice["label"]
        if label:
            locator = self.page.get_by_label(label, exact=False)
            if await locator.count() > 0:
                candidate_locator = locator.first
                locator_type = (await self._safe_get_attribute(candidate_locator, 'type') or '').strip().lower()
                if locator_type == input_type:
                    return candidate_locator
        return None

    async def _set_greenhouse_file(self, binding: FormFieldBinding) -> tuple[bool, str | None]:
        if binding.artifact_binding is None:
            return False, None
        located: tuple[Any, str | None] | None = None
        for strategy, locator in self._file_locators(binding):
            if await locator.count() == 0:
                continue
            located = (locator.first, strategy)
            break
        if located is None:
            return False, None
        locator, strategy = located
        try:
            await locator.set_input_files(binding.artifact_binding.path)
        except Exception as exc:
            raise GreenhouseBindingError("file_upload_failed", f"File upload failed for {binding.prompt_text}: {exc}") from exc
        return True, strategy

    async def _complete_location_autocomplete(self, binding: FormFieldBinding, locator: Any, search_value: str) -> None:
        await _sleep_ms(350)
        candidates = self._dedupe_strings([search_value, binding.value, search_value.split(",")[0] if search_value else None, (binding.value or "").split(",")[0]])
        for candidate in candidates:
            if await self._click_combobox_option(locator, candidate):
                return
        if await self._locator_has_autocomplete(locator):
            for candidate in candidates:
                try:
                    await locator.fill(candidate)
                except Exception:
                    pass
                await _sleep_ms(150)
                if await self._click_combobox_option(locator, candidate):
                    return
            for key in ("ArrowDown", "Enter"):
                try:
                    await locator.press(key)
                except Exception:
                    continue
                await _sleep_ms(250)
                if await self._combobox_has_selection(locator):
                    return
            raise GreenhouseBindingError(
                "location_autocomplete_failed",
                f"Location autocomplete could not be resolved for {binding.prompt_text}.",
            )

    async def _click_first_locator(self, *selectors: str) -> bool:
        for selector in selectors:
            locator = self.page.locator(selector)
            if await locator.count() == 0:
                continue
            await locator.first.click()
            return True
        return False

    async def _locator_has_autocomplete(self, locator: Any) -> bool:
        for attr in ("role", "aria-autocomplete", "autocomplete", "class"):
            value = await self._safe_get_attribute(locator, attr)
            lowered = (value or "").lower()
            if lowered in {"combobox", "list"} or "autocomplete" in lowered or "typeahead" in lowered:
                return True
        return False

    async def _greenhouse_locator(self, binding: FormFieldBinding) -> tuple[Any | None, str | None]:
        label = (binding.prompt_text or "").strip()
        if label:
            locator = self.page.get_by_label(label, exact=False)
            if await locator.count() > 0:
                return locator.first, "label"

        for name in self._name_candidates(binding):
            locator = self.page.locator(f"[name={self._css_string(name)}]")
            if await locator.count() > 0:
                return locator.first, "name"
            locator = self.page.locator(f"[id={self._css_string(name)}]")
            if await locator.count() > 0:
                return locator.first, "id"

        if self._is_location_field(binding):
            for selector in (
                "input[name*='location']",
                "input[id*='location']",
                "input[autocomplete='address-level2']",
                "input[placeholder*='Location']",
                "input[aria-label*='Location']",
            ):
                locator = self.page.locator(selector)
                if await locator.count() > 0:
                    return locator.first, "location_fallback"
        return None, None

    def _file_locators(self, binding: FormFieldBinding) -> list[tuple[str, Any]]:
        locators: list[tuple[str, Any]] = []
        for name in self._name_candidates(binding):
            locators.append(
                (
                    "file_name",
                    self.page.locator(f"input[type='file'][name={self._css_string(name)}]"),
                )
            )
        lowered = f"{binding.prompt_text} {binding.source_field_name}".lower()
        if "resume" in lowered:
            locators.append(("resume_file", self.page.locator("input[type='file'][name*='resume'], input[type='file'][id*='resume']")))
        if "cover" in lowered:
            locators.append(
                ("cover_letter_file", self.page.locator("input[type='file'][name*='cover'], input[type='file'][id*='cover']"))
            )
        if binding.prompt_text:
            locators.append(("file_label", self.page.get_by_label(binding.prompt_text, exact=False)))
        locators.append(("file_any", self.page.locator("input[type='file']")))
        return locators

    async def _collect_form_state(self) -> dict[str, Any]:
        state = await self.page.evaluate(
            """
            () => {
              const compact = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const isVisible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const unique = (items) => Array.from(new Set(items.filter(Boolean)));
              const isErrorText = (text) => /(required|error|invalid|please|must|upload|\bselect\b)/i.test(text);
              const isReactRequiredProxy = (el) => {
                const className = compact(String(el.className || '')).toLowerCase();
                return compact(el.getAttribute('aria-hidden')) === 'true'
                  && compact(el.getAttribute('tabindex')) === '-1'
                  && className.includes('requiredinput');
              };
              const comboboxHasSelection = (el) => {
                const wrapper = el.closest('.select-shell, .select__container, .select, .phone-input__country');
                if (!wrapper) return false;
                if (wrapper.querySelector('.select__single-value, .select__multi-value')) return true;
                const valueContainer = wrapper.querySelector('.select__value-container');
                if (valueContainer && /has-value/.test(String(valueContainer.className || '').toLowerCase())) return true;
                return false;
              };
              const labelFor = (el) => {
                const labels = el.labels ? Array.from(el.labels).map((node) => compact(node.textContent)) : [];
                const aria = compact(el.getAttribute('aria-label'));
                const placeholder = compact(el.getAttribute('placeholder'));
                const wrapper = el.closest('fieldset, .application-question, .field, .question, .application-field, li, div');
                const heading = wrapper ? wrapper.querySelector('label, legend, .application-label, .field-label, strong, h1, h2, h3') : null;
                return labels.find(Boolean) || aria || compact(heading ? heading.textContent : '') || placeholder || compact(el.getAttribute('name')) || compact(el.id);
              };
              const wrapperErrors = (el) => {
                const wrapper = el.closest('fieldset, .application-question, .field, .question, .application-field, li, div') || el.parentElement;
                const texts = wrapper
                  ? Array.from(wrapper.querySelectorAll('.error, .errors, .field-error, .application-error, .validation-error, [role="alert"], [aria-live]'))
                      .map((node) => compact(node.textContent))
                      .filter((text) => text && isErrorText(text))
                  : [];
                if (typeof el.matches === 'function' && el.matches(':invalid') && compact(el.validationMessage)) {
                  texts.push(compact(el.validationMessage));
                }
                return unique(texts);
              };

              const grouped = new Map();
              for (const el of Array.from(document.querySelectorAll('input, textarea, select'))) {
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image' || type === 'reset') continue;
                if (isReactRequiredProxy(el)) continue;
                const visible = isVisible(el) || (type === 'file' && !!el.closest('label, .application-question, .field, .question, .application-field'));
                if (!visible) continue;
                const name = compact(el.getAttribute('name')) || compact(el.id) || labelFor(el);
                const key = (type === 'checkbox' || type === 'radio') && name ? `${type}:${name}` : `${tag}:${name}`;
                if (!grouped.has(key)) {
                  grouped.set(key, {
                    name,
                    prompt: labelFor(el),
                    widget_type: type || tag,
                    required: false,
                    visible: false,
                    disabled: true,
                    filled: false,
                    error_texts: [],
                  });
                }
                const item = grouped.get(key);
                item.required = item.required || !!el.required || compact(el.getAttribute('aria-required')) === 'true';
                item.visible = item.visible || visible;
                item.disabled = item.disabled && !!(el.disabled || compact(el.getAttribute('aria-disabled')) === 'true');
                item.error_texts = unique(item.error_texts.concat(wrapperErrors(el)));

                if (type === 'checkbox' || type === 'radio') {
                  if (el.checked) item.filled = true;
                  continue;
                }
                if (type === 'file') {
                  const fileCount = el.files ? el.files.length : 0;
                  if (fileCount > 0 || compact(el.value)) item.filled = true;
                  continue;
                }
                if (tag === 'select') {
                  const selected = Array.from(el.selectedOptions || []).map((option) => compact(option.value || option.textContent));
                  if (selected.filter(Boolean).length > 0 && selected.some((value) => value !== '')) item.filled = true;
                  continue;
                }
                const isCombobox = compact(el.getAttribute('role')) === 'combobox' || compact(el.getAttribute('aria-autocomplete')) === 'list';
                if (compact(el.value) || (isCombobox && comboboxHasSelection(el))) item.filled = true;
              }

              const fields = Array.from(grouped.values()).map((field) => ({ ...field, error_texts: unique(field.error_texts) }));
              const missingRequiredControls = fields
                .filter((field) => field.visible && field.required && !field.filled)
                .map((field) => field.prompt || field.name || field.widget_type);
              const visibleValidationErrors = unique([
                ...fields.flatMap((field) => field.error_texts),
                ...Array.from(document.querySelectorAll('.application-error, .error, .errors, .validation-error, [role="alert"], [aria-live]'))
                  .map((node) => compact(node.textContent))
                  .filter((text) => text && isErrorText(text)),
              ]);
              const submitCandidates = Array.from(document.querySelectorAll("#submit_app, button[type='submit'], input[type='submit'], button[data-testid='submit-application'], .ashby-application-form-submit-button"));
              const submit = submitCandidates.find((el) => isVisible(el)) || Array.from(document.querySelectorAll("button, input")).find((el) => {
                if (!isVisible(el)) return false;
                return /submit application/i.test(compact(el.textContent || el.value || ''));
              });
              const formPresent = Array.from(document.querySelectorAll('form')).some((form) => isVisible(form));
              return {
                form_present: formPresent,
                fields,
                missing_required_controls: unique(missingRequiredControls),
                visible_validation_errors: visibleValidationErrors,
                submit_button_present: !!submit,
                submit_button_enabled: submit ? !(submit.disabled || compact(submit.getAttribute('aria-disabled')) === 'true') : null,
                submit_button_text: submit ? compact(submit.textContent || submit.value || '') : '',
              };
            }
            """
        )
        if not isinstance(state, dict):
            return {
                "form_present": True,
                "fields": [],
                "missing_required_controls": [],
                "visible_validation_errors": [],
                "submit_button_present": None,
                "submit_button_enabled": None,
                "submit_button_text": "",
            }
        state.setdefault("form_present", True)
        state.setdefault("fields", [])
        state.setdefault("missing_required_controls", [])
        state.setdefault("visible_validation_errors", [])
        state.setdefault("submit_button_present", None)
        state.setdefault("submit_button_enabled", None)
        state.setdefault("submit_button_text", "")
        return state

    def _classify_pre_submit_failure(self, pre_state: dict[str, Any]) -> tuple[str | None, str]:
        binding_failure = self._specific_binding_failure_reason(pre_state)
        if binding_failure == "location_autocomplete_failed":
            return binding_failure, "Location autocomplete failed before submit"
        if binding_failure == "file_upload_failed":
            return binding_failure, "Required file upload failed before submit"
        if any(audit.get("required") and audit.get("status") in {"missing", "error"} for audit in self.field_audit):
            return "missing_required_bindings", "Required Greenhouse fields could not be bound"
        if pre_state.get("submit_button_present") is False:
            return "submit_button_missing", "Greenhouse submit button was not found"
        if pre_state.get("submit_button_enabled") is False:
            return "submit_button_disabled", "Greenhouse submit button is disabled"
        if pre_state.get("visible_validation_errors") or pre_state.get("missing_required_controls"):
            return "pre_submit_validation_failed", "Pre-submit validation failed"
        return None, ""

    def _specific_binding_failure_reason(self, pre_state: dict[str, Any]) -> str | None:
        for audit in self.field_audit:
            if not audit.get("required"):
                continue
            reason = audit.get("failure_reason")
            if reason in {"location_autocomplete_failed", "file_upload_failed"}:
                return reason
        for item in pre_state.get("missing_required_controls", []):
            lowered = str(item).lower()
            if any(token in lowered for token in ("resume", "cover letter", "cover_letter", "curriculum vitae", "cv")):
                return "file_upload_failed"
            if "location" in lowered:
                return "location_autocomplete_failed"
        return None

    def _manual_email_verification_code(self) -> str | None:
        for binding in self.plan.fields:
            runtime_binding = str(binding.metadata.get("runtime_binding") or "").strip().casefold()
            source_name = str(binding.source_field_name or "").strip().casefold()
            if runtime_binding == "email_verification_code" or source_name == "email_verification_code":
                code = re.sub(r"\s+", "", str(binding.value or ""))
                return code or None
        return None

    async def _email_verification_gate(self) -> dict[str, str] | None:
        try:
            gate_text = ""
            gate_locator = self.page.locator("#email-verification, .email-verification")
            if await gate_locator.count() > 0:
                gate_text = (await gate_locator.first.inner_text()).strip()
            if not gate_text:
                body_text = (await self.page.locator("body").inner_text()).strip()
                lowered_body = body_text.casefold()
                if any(pattern in lowered_body for pattern in _GREENHOUSE_EMAIL_VERIFICATION_PATTERNS):
                    gate_text = body_text
            input_count = await self.page.locator("input[id^='security-input-']").count()
        except Exception:
            return None

        lowered = gate_text.casefold()
        if not gate_text and input_count == 0:
            return None
        if input_count == 0 and not any(pattern in lowered for pattern in _GREENHOUSE_EMAIL_VERIFICATION_PATTERNS):
            return None

        email_match = re.search(r"verification code was sent to\s+([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", gate_text, re.IGNORECASE)
        destination = email_match.group(1) if email_match else ""
        message = "Email verification code required"
        if destination:
            message = f"Email verification code required; code sent to {destination}"
        return {"message": message, "destination": destination}

    async def _fill_email_verification_code(self, code: str) -> bool:
        cleaned = re.sub(r"\s+", "", str(code or ""))
        if not cleaned:
            return False
        inputs = self.page.locator("input[id^='security-input-']")
        count = await inputs.count()
        if count == 0:
            return False
        for index in range(count):
            char = cleaned[index] if index < len(cleaned) else ""
            await inputs.nth(index).fill(char)
        return True

    async def _retry_submit_with_email_verification_code(
        self,
        *,
        initial_url: str,
        pre_state: dict[str, Any],
        code: str,
    ) -> dict[str, Any]:
        filled = await self._fill_email_verification_code(code)
        if not filled:
            return {
                "submitted": False,
                "message": "Email verification code input was not available",
                "failure_reason": "email_verification_required",
                "confirmation_text": None,
                "confirmation_strategy": "email_verification_gate_missing_inputs",
                "matched_confirmation_markers": [],
            }

        submit_button = self.page.locator(_GREENHOUSE_SUBMIT_SELECTOR)
        if await submit_button.count() == 0:
            return {
                "submitted": False,
                "message": "Submit button was not available after entering the email verification code",
                "failure_reason": "submit_button_missing",
                "confirmation_text": None,
                "confirmation_strategy": "email_verification_gate_missing_submit",
                "matched_confirmation_markers": [],
            }

        await submit_button.first.click()
        await _sleep_ms(1500)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            await _sleep_ms(1000)

        if self.capture_policy.screenshots != CaptureMode.OFF:
            await self.page.screenshot(path=str(self.artifacts.final_snapshot_path), full_page=True)
        if self.capture_policy.dom_snapshots != CaptureMode.OFF:
            await self._write_dom_snapshot(self.artifacts.post_submit_dom_path)
        post_state = await self._collect_form_state()
        return await self._classify_post_submit_result(initial_url, post_state)

    async def _classify_post_submit_result(self, initial_url: str, post_state: dict[str, Any]) -> dict[str, Any]:
        confirmation_text, confirmation_markers = await self._explicit_confirmation_text()
        if confirmation_text:
            return {
                "submitted": True,
                "message": "Submitted",
                "failure_reason": None,
                "confirmation_text": confirmation_text,
                "confirmation_strategy": "explicit_success_text",
                "matched_confirmation_markers": confirmation_markers,
            }

        final_url = getattr(self.page, "url", "")
        if final_url != initial_url:
            matched_url_markers = [marker for marker in _GREENHOUSE_SUCCESS_URL_MARKERS if marker in final_url.lower()]
            if matched_url_markers:
                return {
                    "submitted": True,
                    "message": "Submitted",
                    "failure_reason": None,
                    "confirmation_text": final_url,
                    "confirmation_strategy": "url_transition",
                    "matched_confirmation_markers": matched_url_markers,
                }

        container_text, container_markers = await self._confirmation_container_text()
        if not post_state.get("form_present", True) and container_text:
            return {
                "submitted": True,
                "message": "Submitted",
                "failure_reason": None,
                "confirmation_text": container_text,
                "confirmation_strategy": "form_disappeared_confirmation_container",
                "matched_confirmation_markers": container_markers,
            }

        verification_gate = await self._email_verification_gate()
        if verification_gate is not None:
            return {
                "submitted": False,
                "message": verification_gate["message"],
                "failure_reason": "email_verification_required",
                "confirmation_text": verification_gate.get("destination") or None,
                "confirmation_strategy": "email_verification_gate",
                "matched_confirmation_markers": [],
            }

        if post_state.get("visible_validation_errors") or post_state.get("missing_required_controls"):
            return {
                "submitted": False,
                "message": "Post-submit validation errors were visible",
                "failure_reason": "post_submit_validation_error",
                "confirmation_text": None,
                "confirmation_strategy": None,
                "matched_confirmation_markers": [],
            }

        return {
            "submitted": False,
            "message": "Submission outcome could not be confirmed",
            "failure_reason": "confirmation_not_detected",
            "confirmation_text": None,
            "confirmation_strategy": None,
            "matched_confirmation_markers": [],
        }

    async def _explicit_confirmation_text(self) -> tuple[str | None, list[str]]:
        matched: list[str] = []
        for pattern in _GREENHOUSE_SUCCESS_TEXT_PATTERNS:
            locator = self.page.locator(pattern)
            if await locator.count() == 0:
                continue
            matched.append(pattern)
            try:
                text = await locator.first.inner_text()
            except Exception:
                text = pattern
            return text or pattern, matched
        return None, matched

    async def _confirmation_container_text(self) -> tuple[str | None, list[str]]:
        matched: list[str] = []
        for selector in _GREENHOUSE_CONFIRMATION_SELECTORS:
            locator = self.page.locator(selector)
            if await locator.count() == 0:
                continue
            try:
                text = (await locator.first.inner_text()).strip()
            except Exception:
                text = selector
            if text:
                matched.append(selector)
                return text, matched
        return None, matched

    async def _write_dom_snapshot(self, path: Path) -> None:
        path.write_text(await self.page.content(), encoding="utf-8")

    def _result(
        self,
        *,
        submitted: bool,
        message: str,
        failure_reason: str | None,
        pre_state: dict[str, Any],
        post_state: dict[str, Any] | None,
        confirmation_text: str | None,
        confirmation_strategy: str | None,
        matched_confirmation_markers: list[str],
        status: JobLifecycleStatus | None = None,
    ) -> SubmissionResult:
        evidence = SubmissionEvidence(
            pre_submit_snapshot_path=str(self.artifacts.pre_submit_snapshot_path) if self.artifacts.pre_submit_snapshot_path.exists() else None,
            final_snapshot_path=str(self.artifacts.final_snapshot_path) if self.artifacts.final_snapshot_path.exists() else None,
            dom_snapshot_path=str(self.artifacts.pre_submit_dom_path) if self.artifacts.pre_submit_dom_path.exists() else None,
            post_submit_dom_snapshot_path=str(self.artifacts.post_submit_dom_path) if self.artifacts.post_submit_dom_path.exists() else None,
            confirmation_text=confirmation_text,
            confirmation_strategy=confirmation_strategy,
            field_audit=list(self.field_audit),
            failure_reason=failure_reason,
            final_url=getattr(self.page, "url", None),
            visible_validation_errors=list((post_state or pre_state).get("visible_validation_errors") or []),
            matched_confirmation_markers=matched_confirmation_markers,
            missing_required_controls=list(pre_state.get("missing_required_controls") or []),
            submit_button_present=pre_state.get("submit_button_present"),
            submit_button_enabled=pre_state.get("submit_button_enabled"),
        )
        snapshot_path = evidence.final_snapshot_path or evidence.pre_submit_snapshot_path
        resolved_status = status or (JobLifecycleStatus.SUBMITTED if submitted else JobLifecycleStatus.SUBMISSION_UNCERTAIN)
        return SubmissionResult(
            status=resolved_status,
            submitted=submitted,
            uncertain=resolved_status == JobLifecycleStatus.SUBMISSION_UNCERTAIN and not submitted,
            message=message,
            snapshot_path=snapshot_path,
            plan=self.plan,
            evidence=evidence,
        )

    def _choice_candidates(self, binding: FormFieldBinding) -> list[dict[str, str]]:
        option_details = list(binding.metadata.get("option_details") or [])
        raw_values = self._dedupe_strings([*binding.option_values, binding.option_value, *binding.values, binding.value])
        resolved: list[dict[str, str]] = []
        for raw in raw_values:
            match = None
            normalized_raw = self._normalize_choice(raw)
            for option in option_details:
                label = str(option.get("label") or "")
                value = str(option.get("value") or option.get("id") or label)
                if normalized_raw in {self._normalize_choice(label), self._normalize_choice(value)}:
                    match = {"label": label or raw, "value": value or raw}
                    break
            if match is None:
                match = {"label": raw, "value": raw}
            if match not in resolved:
                resolved.append(match)
        if not resolved and _is_demographic_binding(binding):
            fallback = _preferred_decline_option(option_details)
            if fallback is not None:
                resolved.append(fallback)
        return resolved

    def _name_candidates(self, binding: FormFieldBinding) -> list[str]:
        submission_binding = dict(binding.metadata.get("submission_binding") or {})
        names = [binding.source_field_name, submission_binding.get("name"), submission_binding.get("id")]
        if binding.source_field_name and not binding.source_field_name.startswith("job_application["):
            names.append(f"job_application[{binding.source_field_name}]")
        binding_name = str(submission_binding.get("name") or "").strip()
        if binding_name and not binding_name.startswith("job_application["):
            names.append(f"job_application[{binding_name}]")
        return self._dedupe_strings(names)

    @staticmethod
    def _preferred_location_search_value(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return text
        city = text.split(",", 1)[0].strip()
        return city or text

    def _is_location_field(self, binding: FormFieldBinding) -> bool:
        if str(binding.metadata.get("section") or "").lower() == "location":
            return True
        lowered = f"{binding.prompt_text} {binding.source_field_name}".lower()
        return "location" in lowered

    def _value_summary(self, binding: FormFieldBinding) -> str | None:
        if binding.artifact_binding is not None:
            return Path(binding.artifact_binding.path).name
        values = self._dedupe_strings([*binding.values, binding.value, *binding.option_values, binding.option_value])
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        return ", ".join(values[:3])

    @staticmethod
    def _dedupe_strings(values: list[str | None]) -> list[str]:
        seen: list[str] = []
        for value in values:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @staticmethod
    def _normalize_choice(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _css_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    async def _safe_get_attribute(locator: Any, name: str) -> str | None:
        try:
            return await locator.get_attribute(name)
        except Exception:
            return None

class PlaywrightSubmitter:
    def __init__(
        self,
        timeout_seconds: int = 30,
        capture_policy: SubmissionCapturePolicy | None = None,
        *,
        browser_attach_enabled: bool = False,
        browser_cdp_url: str | None = None,
        browser_mode: str = "headless",
        max_open_tabs: int = 10,
        captcha_strategy: str = "skip",
        captcha_solver: CaptchaSolver | None = None,
    ) -> None:
        # Headed/attached modes need longer timeouts since real browser rendering is slower
        effective_timeout = timeout_seconds
        resolved_mode = str(browser_mode or "headless").strip().lower() or "headless"
        if resolved_mode in ("headed", "attached") and timeout_seconds <= 30:
            effective_timeout = 60
        self.timeout_ms = effective_timeout * 1000
        self.capture_policy = capture_policy or SubmissionCapturePolicy()
        self.browser_attach_enabled = browser_attach_enabled
        self.browser_cdp_url = str(browser_cdp_url or "").strip() or None
        self.browser_mode = resolved_mode
        self.max_open_tabs = max(1, int(max_open_tabs or 1))
        self.captcha_strategy = captcha_strategy
        self.captcha_solver = captcha_solver

    def _capture_enabled(self, mode: CaptureMode) -> bool:
        return mode != CaptureMode.OFF

    def _browser_operation_timeout_seconds(self) -> int:
        base = max(1, int(self.timeout_ms / 1000))
        # Headed/attached modes need more time for real browser rendering and user-visible interactions
        if self.browser_mode in ("headed", "attached"):
            return max(240, base * 6)
        return max(120, base * 4)

    @staticmethod
    async def _start_playwright_runtime() -> Any:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError(f"Playwright is unavailable: {exc}") from exc
        try:
            return await async_playwright().start()
        except Exception as exc:
            raise RuntimeError(f"Playwright runtime could not start: {exc}") from exc

    @staticmethod
    async def _stop_playwright_runtime(playwright: Any | None) -> None:
        if playwright is None:
            return
        try:
            await playwright.stop()
        except Exception as exc:
            log.debug("    [browser] Playwright stop error: %s", exc)

    @staticmethod
    async def _await_with_timeout(awaitable: Any, timeout_seconds: float) -> Any:
        with anyio.fail_after(timeout_seconds):
            return await awaitable

    @staticmethod
    def _install_playwright_exception_guard() -> tuple[asyncio.AbstractEventLoop | None, Any]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None, None
        previous_handler = loop.get_exception_handler()

        def _handler(loop_obj: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            exc = context.get("exception")
            message = str(context.get("message") or "").strip()
            if (
                message == "Task exception was never retrieved"
                and isinstance(exc, PermissionError)
                and "access is denied" in str(exc).casefold()
            ):
                log.debug("    [browser] Suppressed Playwright startup task exception: %s", exc)
                return
            if previous_handler is not None:
                previous_handler(loop_obj, context)
                return
            loop_obj.default_exception_handler(context)

        loop.set_exception_handler(_handler)
        return loop, previous_handler

    @staticmethod
    def _restore_playwright_exception_guard(loop: asyncio.AbstractEventLoop | None, previous_handler: Any) -> None:
        if loop is None:
            return
        loop.set_exception_handler(previous_handler)

    def _apply_capture_policy(self, result: SubmissionResult) -> SubmissionResult:
        evidence = result.evidence
        if evidence is None:
            return result
        submitted = bool(result.submitted and not result.uncertain)
        if not SubmissionCapturePolicy.should_persist(self.capture_policy.screenshots, submitted=submitted):
            self._delete_if_exists(evidence.pre_submit_snapshot_path)
            self._delete_if_exists(evidence.final_snapshot_path)
            evidence.pre_submit_snapshot_path = None
            evidence.final_snapshot_path = None
            result.snapshot_path = None
        if not SubmissionCapturePolicy.should_persist(self.capture_policy.dom_snapshots, submitted=submitted):
            self._delete_if_exists(evidence.dom_snapshot_path)
            self._delete_if_exists(evidence.post_submit_dom_snapshot_path)
            evidence.dom_snapshot_path = None
            evidence.post_submit_dom_snapshot_path = None
        if not SubmissionCapturePolicy.should_persist(self.capture_policy.traces, submitted=submitted):
            self._delete_if_exists(evidence.trace_path)
            evidence.trace_path = None
            result.trace_path = None
        return result

    async def _attempt_captcha_solve(self, page: Any, dom_html: str, page_url: str) -> bool:
        """Attempt to solve a captcha based on configured strategy.

        Returns True if captcha was solved or bypassed, False if it remains blocking.
        """
        if self.captcha_strategy == "skip":
            log.info("    [captcha] Captcha detected but strategy is 'skip', marking as failure")
            return False

        if self.captcha_strategy == "manual":
            log.info("    [captcha] Captcha detected, strategy is 'manual' — waiting for a human solve in the active browser")
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                try:
                    token_present = await page.evaluate(
                        """() => {
                            const nodes = [
                                document.querySelector('textarea[name="g-recaptcha-response"]'),
                                document.querySelector('textarea#g-recaptcha-response'),
                                document.querySelector('textarea[name="h-captcha-response"]'),
                            ].filter(Boolean);
                            return nodes.some((el) => Boolean(el.value && el.value.length > 10));
                        }"""
                    )
                except Exception:
                    token_present = False
                if token_present:
                    log.info("    [captcha] Human solved the captcha successfully via challenge token")
                    return True
                try:
                    refreshed_html = await page.content()
                except Exception:
                    refreshed_html = ""
                if refreshed_html:
                    refreshed_findings = analyze_dom_snapshot(refreshed_html)
                    if not refreshed_findings.get("has_captcha"):
                        log.info("    [captcha] Captcha challenge is no longer present; continuing submit flow")
                        return True
                await anyio.sleep(2)
            log.warning("    [captcha] Manual captcha solve timed out after 120s")
            return False

        # strategy == "solve"
        if self.captcha_solver is None:
            log.warning("    [captcha] Strategy is 'solve' but no captcha solver configured (missing API key?)")
            return False

        captcha_type = detect_captcha_type(dom_html)
        if captcha_type is None:
            log.warning("    [captcha] Could not determine captcha type from DOM")
            return False

        try:
            if captcha_type == "hcaptcha":
                site_key = extract_hcaptcha_site_key(dom_html)
                if not site_key:
                    log.warning("    [captcha] hCaptcha detected but could not extract site key")
                    return False
                token = await self.captcha_solver.solve_hcaptcha(site_key, page_url)
                await page.evaluate(
                    """(token) => {
                        const textarea = document.querySelector('textarea[name="h-captcha-response"]');
                        if (textarea) { textarea.value = token; }
                        if (typeof hcaptcha !== 'undefined') {
                            try { hcaptcha.execute(); } catch(e) {}
                        }
                    }""",
                    token,
                )
            else:
                site_key = extract_recaptcha_site_key(dom_html)
                if not site_key:
                    log.warning("    [captcha] reCAPTCHA detected but could not extract site key")
                    return False
                token = await self.captcha_solver.solve_recaptcha_v2(site_key, page_url)
                await page.evaluate(
                    """(token) => {
                        const textarea = document.querySelector('textarea#g-recaptcha-response, textarea[name="g-recaptcha-response"]');
                        if (textarea) {
                            textarea.style.display = '';
                            textarea.value = token;
                        }
                        if (typeof grecaptcha !== 'undefined') {
                            try {
                                const cb = grecaptcha.getResponse ? null : null;
                                grecaptcha.enterprise
                                    ? grecaptcha.enterprise.execute()
                                    : (grecaptcha.getResponse() || grecaptcha.execute());
                            } catch(e) {}
                        }
                        // Trigger any callback on the widget
                        const widget = document.querySelector('.g-recaptcha');
                        if (widget) {
                            const callbackName = widget.getAttribute('data-callback');
                            if (callbackName && typeof window[callbackName] === 'function') {
                                window[callbackName](token);
                            }
                        }
                    }""",
                    token,
                )

            log.info("    [captcha] Token injected into page for %s", captcha_type)
            await _sleep_ms(1000)
            return True

        except CaptchaSolverError as exc:
            log.error("    [captcha] Solve failed: %s", exc)
            return False
        except Exception as exc:
            log.error("    [captcha] Unexpected error during captcha solve: %s", exc, exc_info=True)
            return False

    @staticmethod
    def _delete_if_exists(path: str | None) -> None:
        if not path:
            return
        candidate = Path(path)
        if candidate.exists():
            candidate.unlink()

    def _cdp_port(self) -> int:
        return session_cdp_port(self.browser_cdp_url)

    def _attachable_browser_candidates(self) -> list[Path]:
        return session_attachable_browser_candidates()

    def _launch_attachable_browser(self) -> bool:
        return session_launch_attachable_browser(
            browser_cdp_url=self.browser_cdp_url,
            profile_dir=Path.cwd() / '.fmj' / 'browser-attach-profile',
            start_url='about:blank',
        )

    @staticmethod
    def _manual_handoff_profile_dir() -> Path:
        return Path.cwd() / '.fmj' / 'browser-attach-profile'

    def _manual_handoff_cdp_url(self) -> str:
        return str(self.browser_cdp_url or "http://127.0.0.1:9222").strip() or "http://127.0.0.1:9222"

    async def _connect_attached_browser(self, playwright) -> dict[str, Any]:
        session = await session_connect_attached_browser(
            playwright,
            browser_cdp_url=str(self.browser_cdp_url or ''),
        )
        log.info('    [browser] CDP attached successfully (existing_contexts=%d)', 0 if session.get('created_context') else 1)
        return session

    async def _open_browser_session(self, playwright, *, prefer_attached: bool = False) -> dict[str, Any]:
        attach_enabled = bool(self.browser_attach_enabled and self.browser_mode == "attached" and self.browser_cdp_url)
        log.info("    [browser] Opening session: mode=%s, attach_enabled=%s, cdp_url=%s",
                 self.browser_mode, attach_enabled, self.browser_cdp_url)
        profile_dir = self._manual_handoff_profile_dir()
        if prefer_attached:
            handoff_cdp_url = self._manual_handoff_cdp_url()
            try:
                log.info("    [browser] keep_browser_open requested; preferring attached browser on %s", handoff_cdp_url)
                return await session_open_browser_session(
                    playwright,
                    browser_mode="attached",
                    browser_attach_enabled=True,
                    browser_cdp_url=handoff_cdp_url,
                    launch_if_missing=True,
                    profile_dir=profile_dir,
                    start_url='about:blank',
                    accept_downloads=False,
                )
            except Exception as exc:
                log.warning("    [browser] Attached handoff browser unavailable, falling back to %s mode: %s", self.browser_mode, exc)
        return await session_open_browser_session(
            playwright,
            browser_mode=self.browser_mode,
            browser_attach_enabled=self.browser_attach_enabled,
            browser_cdp_url=self.browser_cdp_url,
            launch_if_missing=True,
            profile_dir=profile_dir,
            start_url='about:blank',
            accept_downloads=False,
        )

    @staticmethod
    def _remove_page_listeners(page: Any, listeners: dict[str, Any]) -> None:
        """Remove registered event listeners from a Playwright page to prevent cleanup hangs."""
        if page is None:
            return
        for event_name, handler in listeners.items():
            try:
                page.remove_listener(event_name, handler)
            except Exception:
                pass

    async def _close_browser_session(self, session: dict[str, Any]) -> None:
        # Remove any registered event listeners before closing
        for handler_info in session.get("_listeners", []):
            self._remove_page_listeners(session.get("page"), handler_info)
        try:
            await self._await_with_timeout(session_close_browser_session(session), 5)
        except Exception as exc:
            log.debug("    [browser] session close error: %s", exc)

    @staticmethod
    def _mark_browser_left_open(result: SubmissionResult, *, browser_left_open: bool) -> SubmissionResult:
        if result.evidence is None:
            result.evidence = SubmissionEvidence()
        result.evidence.browser_left_open = bool(browser_left_open)
        return result

    async def inspect_form(self, url: str) -> list[dict[str, Any]]:
        loop, previous_handler = self._install_playwright_exception_guard()
        playwright = await self._start_playwright_runtime()
        session: dict[str, Any] | None = None
        try:
            session = await self._open_browser_session(playwright)
            page = session["page"]
            page.set_default_timeout(self.timeout_ms)
            try:
                await page.goto(url, wait_until="networkidle")
            except Exception:
                log.info("    [browser] inspect_form networkidle timeout, retrying with domcontentloaded")
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    await _sleep_ms(1500)
                except Exception as exc:
                    log.error("    [browser] inspect_form navigation failed: %s", exc)
                    return []
            fields = await page.evaluate(
                """
                () => {
                  const rows = [];
                  const controls = Array.from(document.querySelectorAll('input, textarea, select'));
                  const textOf = (node) => (node?.textContent || '').replace(/\\s+/g, ' ').trim();
                  const leverCardTemplates = (() => {
                    const templates = new Map();
                    const hiddenInputs = Array.from(document.querySelectorAll('input[type="hidden"][name^="cards["][name$="[baseTemplate]"]'));
                    for (const input of hiddenInputs) {
                      const name = (input.getAttribute('name') || '').trim();
                      const match = name.match(/^cards\\[([^\\]]+)\\]\\[baseTemplate\\]$/);
                      if (!match) continue;
                      try {
                        const payload = JSON.parse(input.value || '{}');
                        templates.set(match[1], payload);
                      } catch (_error) {
                        continue;
                      }
                    }
                    return templates;
                  })();
                  const leverTemplateFieldFor = (bindingName) => {
                    const match = String(bindingName || '').match(/^cards\\[([^\\]]+)\\]\\[field(\\d+)\\]$/);
                    if (!match) return null;
                    const [, cardId, fieldIndexRaw] = match;
                    const template = leverCardTemplates.get(cardId);
                    if (!template || !Array.isArray(template.fields)) return null;
                    const fieldIndex = Number(fieldIndexRaw);
                    const field = template.fields[fieldIndex];
                    if (!field || typeof field !== 'object') return null;
                    const options = Array.isArray(field.options)
                      ? field.options
                          .map((option) => ({
                            label: textOf({ textContent: option?.text || option?.label || option?.value || option?.optionId || '' }),
                            value: String(option?.text || option?.value || option?.optionId || option?.label || '').trim(),
                          }))
                          .filter((option) => option.label || option.value)
                      : [];
                    return {
                      card_label: String(template.text || '').replace(/\\s+/g, ' ').trim(),
                      field_label: String(field.text || '').replace(/\\s+/g, ' ').trim(),
                      field_description: String(field.description || '').replace(/\\s+/g, ' ').trim(),
                      options,
                    };
                  };
                  const optionLabelFor = (el) => {
                    if (el.labels && el.labels.length) {
                      return Array.from(el.labels).map((node) => textOf(node)).filter(Boolean).join(' ');
                    }
                    return textOf(el.closest('label'));
                  };
                  const groupLabelFor = (el, bindingName) => {
                    const templateField = leverTemplateFieldFor(bindingName);
                    if (templateField?.field_label) return templateField.field_label;
                    const ariaLabelledBy = (el.getAttribute('aria-labelledby') || '').trim();
                    if (ariaLabelledBy) {
                      const labelled = ariaLabelledBy
                        .split(/\\s+/)
                        .map((id) => textOf(document.getElementById(id)))
                        .filter(Boolean);
                      if (labelled.length) return labelled.join(' ');
                    }
                    const fieldset = el.closest('fieldset');
                    const legendText = textOf(fieldset?.querySelector('legend'));
                    if (legendText) return legendText;
                    const optionLabels = new Set();
                    if (bindingName) {
                      const escapedName = String(bindingName).replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\"');
                      const peers = Array.from(document.querySelectorAll(`input[name="${escapedName}"]`));
                      for (const peer of peers) {
                        if (!peer.labels) continue;
                        for (const labelNode of Array.from(peer.labels)) {
                          const text = textOf(labelNode);
                          if (text) optionLabels.add(text);
                        }
                      }
                    }
                    const wrapper = el.closest('li, .application-question, .posting-requirements, .application-field, fieldset');
                    const candidates = wrapper
                      ? Array.from(wrapper.querySelectorAll('legend, .application-label, .posting-requirements-label, strong, h1, h2, h3, h4, h5, h6, label'))
                          .map((node) => textOf(node))
                          .filter(Boolean)
                          .filter((text) => !optionLabels.has(text))
                      : [];
                    return candidates[0] || '';
                  };
                  const labelFor = (el) => {
                    return groupLabelFor(el, el.getAttribute('name') || el.getAttribute('id') || '') || optionLabelFor(el);
                  };
                  for (const el of controls) {
                    const tag = el.tagName.toLowerCase();
                    const type = el.getAttribute('type') || '';
                    const id = el.getAttribute('id') || '';
                    const name = el.getAttribute('name') || '';
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const ariaAutocomplete = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
                    const bindingName = name || id || '';
                    const isCombobox = role === 'combobox' || ariaAutocomplete === 'list';
                    if (!bindingName && type !== 'radio' && type !== 'checkbox') continue;
                    if (type === 'hidden' || type === 'submit') continue;
                    const label = labelFor(el) || el.getAttribute('aria-label') || el.getAttribute('placeholder') || bindingName;
                    const accept = (el.getAttribute('accept') || '').split(',').map((part) => part.trim()).filter(Boolean);
                    if (tag === 'select') {
                      const templateField = leverTemplateFieldFor(bindingName);
                      const options = Array.from(el.querySelectorAll('option')).map((option) => ({
                        option_label: option.textContent?.trim() || '',
                        option_value: option.value || option.textContent?.trim() || '',
                      }));
                      const mergedOptions = templateField?.options?.length
                        ? templateField.options.map((option) => ({
                            option_label: option.label || option.value || '',
                            option_value: option.value || option.label || '',
                          }))
                        : options;
                      rows.push({
                        tag,
                        type,
                        field_type: 'select',
                        widget_type: 'select',
                        name: bindingName,
                        id,
                        required: el.required,
                        label: templateField?.field_label || label,
                        accept,
                        options: mergedOptions,
                        source_snapshot_ref: `${tag}:${bindingName}`,
                      });
                      continue;
                    }
                    if (type === 'checkbox' || type === 'radio') {
                      const templateField = leverTemplateFieldFor(bindingName);
                      const optionLabel = optionLabelFor(el) || label;
                      const groupLabel = groupLabelFor(el, bindingName) || optionLabel || bindingName;
                      const templateOptions = templateField?.options || [];
                      const matchingTemplateOption = templateOptions.find((option) => {
                        const templateValue = String(option.value || option.label || '').trim().toLowerCase();
                        const optionValue = String(el.value || optionLabel || '').trim().toLowerCase();
                        return templateValue && optionValue && templateValue === optionValue;
                      });
                      rows.push({
                        tag,
                        type,
                        field_type: type,
                        widget_type: type === 'checkbox' ? 'checkbox_group' : 'radio_group',
                        name: bindingName,
                        id,
                        required: el.required,
                        label: groupLabel,
                        group_label: groupLabel,
                        option_label: matchingTemplateOption?.label || optionLabel,
                        option_value: matchingTemplateOption?.value || el.value || optionLabel || groupLabel,
                        option_details: templateOptions.map((option) => ({
                          label: option.label || option.value || '',
                          value: option.value || option.label || '',
                        })),
                        accept,
                        source_snapshot_ref: `${type}:${bindingName}`,
                      });
                      continue;
                    }
                    const templateField = leverTemplateFieldFor(bindingName);
                    rows.push({
                      tag,
                      type,
                      field_type: isCombobox ? 'select' : (type || tag),
                      widget_type: tag === 'textarea' ? 'textarea' : (isCombobox ? 'select' : (type || tag || 'text')),
                      name: bindingName,
                      id,
                      required: el.required,
                      label: templateField?.field_label || label,
                      description: templateField?.field_description || '',
                      option_details: (templateField?.options || []).map((option) => ({
                        label: option.label || option.value || '',
                        value: option.value || option.label || '',
                      })),
                      accept,
                      source_snapshot_ref: `${tag}:${bindingName}`,
                    });
                  }
                  return rows;
                }
                """
            )
            return fields
        finally:
            if session is not None:
                await self._close_browser_session(session)
            await self._stop_playwright_runtime(playwright)
            self._restore_playwright_exception_guard(loop, previous_handler)

    async def inspect_lever_form(self, url: str) -> list[dict[str, Any]]:
        return await self.inspect_form(url)

    async def submit_greenhouse(self, url: str, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        return await self._run_greenhouse(url, plan, output_dir, preview_only=False)

    async def preview_greenhouse(
        self,
        url: str,
        plan: SubmissionPlan,
        output_dir: Path,
        *,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        return await self._run_greenhouse(url, plan, output_dir, preview_only=True, keep_browser_open=keep_browser_open)

    async def submit_lever(self, url: str, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        return await self.submit_generic_form(url, plan, output_dir)

    async def submit_generic_form(self, url: str, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        return await self._submit_generic(url, plan, output_dir, submit_selector=_GENERIC_SUBMIT_SELECTOR, preview_only=False)

    async def preview_generic_form(
        self,
        url: str,
        plan: SubmissionPlan,
        output_dir: Path,
        *,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        return await self._submit_generic(
            url,
            plan,
            output_dir,
            submit_selector=_GENERIC_SUBMIT_SELECTOR,
            preview_only=True,
            keep_browser_open=keep_browser_open,
        )

    async def _run_greenhouse(
        self,
        url: str,
        plan: SubmissionPlan,
        output_dir: Path,
        *,
        preview_only: bool,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        log.info("    [browser] Starting Greenhouse %s to: %s", "preview" if preview_only else "submission", url)
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        except Exception:
            class PlaywrightTimeoutError(Exception):
                pass

        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = GreenhouseArtifactPaths(
            pre_submit_snapshot_path=output_dir / "pre-submit.png",
            final_snapshot_path=output_dir / "submit-final.png",
            trace_path=output_dir / "submit-trace.zip",
            pre_submit_dom_path=output_dir / "submit-dom-before.html",
            post_submit_dom_path=output_dir / "submit-dom-after.html",
        )
        network_errors: list[str] = []
        flow: GreenhouseHostedFormFlow | None = None

        async def _run_session() -> SubmissionResult:
            nonlocal flow
            loop, previous_handler = self._install_playwright_exception_guard()
            playwright = await self._start_playwright_runtime()
            session: dict[str, Any] | None = None
            context = None
            page = None
            tracing_started = False
            _on_request_failed = None
            _on_console = None
            browser_left_open = False
            try:
                session = await self._open_browser_session(playwright, prefer_attached=keep_browser_open and preview_only)
                browser_left_open = bool(keep_browser_open and preview_only and session.get("attached"))
                context = session["context"]
                page = session["page"]
                page.set_default_timeout(self.timeout_ms)

                def _on_request_failed(request):
                    network_errors.append(f"requestfailed:{request.url}:{request.failure}")

                def _on_console(message):
                    if message.type == "error":
                        network_errors.append(f"console:{message.type}:{message.text}")

                page.on("requestfailed", _on_request_failed)
                page.on("console", _on_console)
                session["_listeners"] = [{"requestfailed": _on_request_failed, "console": _on_console}]

                try:
                    if self._capture_enabled(self.capture_policy.traces):
                        await context.tracing.start(screenshots=True, snapshots=True)
                        tracing_started = True
                    flow = GreenhouseHostedFormFlow(page, plan, artifacts, self.capture_policy)
                    log.info("    [browser] Navigating to form and filling fields...")
                    result = await (flow.preview(url) if preview_only else flow.submit(url))
                    log.info(
                        "    [browser] Form %s complete: status=%s, submitted=%s",
                        "preview" if preview_only else "submission",
                        result.status.value,
                        result.submitted,
                    )
                    return self._mark_browser_left_open(result, browser_left_open=browser_left_open)
                finally:
                    if page is not None and _on_request_failed is not None and _on_console is not None:
                        self._remove_page_listeners(page, {"requestfailed": _on_request_failed, "console": _on_console})
                    try:
                        if tracing_started:
                            await self._await_with_timeout(context.tracing.stop(path=str(artifacts.trace_path)), 10)
                    except Exception as exc:
                        network_errors.append(f"trace_stop:{exc}")
                    if keep_browser_open and preview_only and session is not None and session.get("attached"):
                        log.info("    [browser] Leaving preview tab open for manual completion.")
                    elif keep_browser_open and preview_only:
                        network_errors.append("manual_handoff_unavailable:attached_browser_required")
                        log.warning("    [browser] keep_browser_open requested but the preview session is not attached; closing browser instead.")
                        try:
                            if session is not None:
                                await self._await_with_timeout(self._close_browser_session(session), 10)
                        except Exception as exc:
                            network_errors.append(f"browser_close:{exc}")
                    else:
                        try:
                            if session is not None:
                                await self._await_with_timeout(self._close_browser_session(session), 10)
                        except Exception as exc:
                            network_errors.append(f"browser_close:{exc}")
            finally:
                await self._stop_playwright_runtime(playwright)
                self._restore_playwright_exception_guard(loop, previous_handler)

        try:
            result = await self._await_with_timeout(_run_session(), self._browser_operation_timeout_seconds())
            if result.evidence is None:
                result.evidence = SubmissionEvidence()
            result.evidence.trace_path = str(artifacts.trace_path) if artifacts.trace_path.exists() else None
            result.evidence.network_errors = list(network_errors)
            result.trace_path = result.evidence.trace_path
            return self._apply_capture_policy(result)
        except PlaywrightTimeoutError as exc:
            log.error("    [browser] Playwright TIMEOUT: %s", exc)
            return self._apply_capture_policy(self._greenhouse_exception_result(
                plan,
                artifacts,
                message=str(exc),
                failure_reason="playwright_timeout",
                network_errors=network_errors,
                field_audit=flow.field_audit if flow is not None else [],
            ))
        except (asyncio.TimeoutError, TimeoutError):
            message = f"Browser preview exceeded {self._browser_operation_timeout_seconds()}s overall timeout"
            log.error("    [browser] Browser operation TIMEOUT: %s", message)
            return self._apply_capture_policy(self._greenhouse_exception_result(
                plan,
                artifacts,
                message=message,
                failure_reason="browser_operation_timeout",
                network_errors=network_errors,
                field_audit=flow.field_audit if flow is not None else [],
            ))
        except RuntimeError as exc:
            message = str(exc)
            failure_reason = "playwright_runtime_blocked" if "could not start" in message.lower() else "submit_exception"
            log.error("    [browser] Playwright runtime blocked: %s", message)
            return self._apply_capture_policy(self._greenhouse_exception_result(
                plan,
                artifacts,
                message=message,
                failure_reason=failure_reason,
                network_errors=network_errors,
                field_audit=flow.field_audit if flow is not None else [],
            ))
        except Exception as exc:
            log.error("    [browser] Submission EXCEPTION: %s", exc, exc_info=True)
            return self._apply_capture_policy(self._greenhouse_exception_result(
                plan,
                artifacts,
                message=str(exc),
                failure_reason="submit_exception",
                network_errors=network_errors,
                field_audit=flow.field_audit if flow is not None else [],
            ))

    def _greenhouse_exception_result(
        self,
        plan: SubmissionPlan,
        artifacts: GreenhouseArtifactPaths,
        *,
        message: str,
        failure_reason: str,
        network_errors: list[str],
        field_audit: list[dict[str, Any]],
    ) -> SubmissionResult:
        evidence = SubmissionEvidence(
            pre_submit_snapshot_path=str(artifacts.pre_submit_snapshot_path) if artifacts.pre_submit_snapshot_path.exists() else None,
            final_snapshot_path=str(artifacts.final_snapshot_path) if artifacts.final_snapshot_path.exists() else None,
            trace_path=str(artifacts.trace_path) if artifacts.trace_path.exists() else None,
            dom_snapshot_path=str(artifacts.pre_submit_dom_path) if artifacts.pre_submit_dom_path.exists() else None,
            post_submit_dom_snapshot_path=str(artifacts.post_submit_dom_path) if artifacts.post_submit_dom_path.exists() else None,
            field_audit=field_audit,
            failure_reason=failure_reason,
            network_errors=list(network_errors),
        )
        return SubmissionResult(
            status=JobLifecycleStatus.SUBMISSION_UNCERTAIN,
            submitted=False,
            uncertain=True,
            message=message,
            snapshot_path=evidence.final_snapshot_path or evidence.pre_submit_snapshot_path,
            trace_path=evidence.trace_path,
            plan=plan,
            evidence=evidence,
        )

    async def _submit_generic(
        self,
        url: str,
        plan: SubmissionPlan,
        output_dir: Path,
        submit_selector: str,
        *,
        preview_only: bool,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        except Exception:
            class PlaywrightTimeoutError(Exception):
                pass

        output_dir.mkdir(parents=True, exist_ok=True)
        pre_submit_path = output_dir / "pre-submit.png"
        final_snapshot_path = output_dir / "submit-final.png"
        trace_path = output_dir / "submit-trace.zip"
        dom_snapshot_path = output_dir / "submit-dom-before.html"
        post_dom_snapshot_path = output_dir / "submit-dom-after.html"
        network_errors: list[str] = []
        field_audit: list[dict[str, Any]] = []
        supported_widgets = {"text", "textarea", "select", "dropdown", "checkbox", "checkbox_group", "radio", "radio_group", "file", "date", "email", "tel", "url"}
        playwright = None

        try:
            loop, previous_handler = self._install_playwright_exception_guard()
            playwright = await self._start_playwright_runtime()
            session: dict[str, Any] | None = None
            context = None
            page = None
            tracing_started = False
            _listener_map: dict[str, Any] = {}
            browser_left_open = False
            try:
                session = await self._open_browser_session(playwright, prefer_attached=keep_browser_open and preview_only)
                browser_left_open = bool(keep_browser_open and preview_only and session.get("attached"))
                context = session["context"]
                page = session["page"]
                page.set_default_timeout(self.timeout_ms)

                def _on_request_failed(request):
                    network_errors.append(f"requestfailed:{request.url}:{request.failure}")

                def _on_console(message):
                    if message.type == "error":
                        network_errors.append(f"console:{message.type}:{message.text}")

                page.on("requestfailed", _on_request_failed)
                page.on("console", _on_console)
                _listener_map = {"requestfailed": _on_request_failed, "console": _on_console}
                session["_listeners"] = [_listener_map]

                try:
                    if self._capture_enabled(self.capture_policy.traces):
                        await context.tracing.start(screenshots=True, snapshots=True)
                        tracing_started = True
                    try:
                        await page.goto(url, wait_until="domcontentloaded")
                    except Exception as nav_exc:
                        log.warning("    [browser] Navigation to %s failed: %s. Retrying...", url[:80], nav_exc)
                        try:
                            await page.goto(url, wait_until="commit", timeout=90000)
                        except Exception as retry_exc:
                            log.error("    [browser] Navigation retry also failed: %s", retry_exc)
                            raise
                    try:
                        await page.wait_for_load_state("networkidle")
                    except Exception:
                        await _sleep_ms(1500)

                    initial_html = await page.content()
                    dom_findings = analyze_dom_snapshot(initial_html)
                    unsupported_required = [
                        binding.prompt_text or binding.source_field_name or "unknown field"
                        for binding in plan.fields
                        if binding.required and binding.widget_type not in supported_widgets and binding.artifact_binding is None
                    ]
                    failure_reason = None
                    message = None
                    if dom_findings.get("has_login_wall"):
                        failure_reason = "login_wall_detected"
                        message = "Apply flow requires login or account creation"
                    elif dom_findings.get("has_captcha"):
                        captcha_resolved = await self._attempt_captcha_solve(page, initial_html, url)
                        if not captcha_resolved:
                            failure_reason = "captcha_detected"
                            message = "Apply flow is blocked by captcha or anti-bot controls"
                        else:
                            log.info("    [browser] Captcha solved successfully, continuing with form fill")
                    elif unsupported_required:
                        failure_reason = "unsupported_field_types"
                        message = "Required fields use unsupported widget types"

                    if failure_reason is None:
                        for binding in plan.fields:
                            try:
                                result = await self._apply_generic_binding(page, binding)
                            except Exception as exc:
                                log.warning("    [form] Unhandled generic binding failure for field %s: %s", binding.source_field_name, exc)
                                result = {
                                    "field": binding.source_field_name,
                                    "prompt": binding.prompt_text,
                                    "widget_type": binding.widget_type,
                                    "required": binding.required,
                                    "status": "error",
                                    "error": str(exc),
                                    "error_type": type(exc).__name__,
                                }
                            field_audit.append(result)
                        await self._second_pass_generic_required_fields(page, plan, field_audit)
                        missing_required_controls = [
                            audit.get("prompt") or audit.get("field") or "unknown field"
                            for audit in field_audit
                            if audit.get("required") and audit.get("status") in {"missing", "error"}
                        ]
                        submit = page.locator(submit_selector)
                        submit_count = await submit.count()
                        submit_enabled = None
                        if submit_count > 0:
                            try:
                                disabled_attr = await submit.first.get_attribute("disabled")
                                submit_enabled = disabled_attr in {None, "", "false", "False"}
                            except Exception:
                                submit_enabled = True
                        if missing_required_controls:
                            failure_reason = "missing_required_bindings"
                            message = "Required fields could not be bound"
                        elif submit_count == 0:
                            failure_reason = "submit_button_missing"
                            message = "Submit button was not found"
                        elif submit_enabled is False:
                            failure_reason = "submit_button_disabled"
                            message = "Submit button is disabled"
                    else:
                        missing_required_controls = []
                        submit_count = 0
                        submit_enabled = None

                    if self.capture_policy.dom_snapshots != CaptureMode.OFF:
                        dom_snapshot_path.write_text(await page.content(), encoding="utf-8")
                    if self.capture_policy.screenshots != CaptureMode.OFF:
                        await page.screenshot(path=str(pre_submit_path), full_page=True)

                    if failure_reason is not None:
                        if tracing_started:
                            await context.tracing.stop(path=str(trace_path))
                            tracing_started = False
                        return self._mark_browser_left_open(self._apply_capture_policy(
                            SubmissionResult(
                                status=JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                                submitted=False,
                                uncertain=True,
                                message=message,
                                snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                                trace_path=str(trace_path) if trace_path.exists() else None,
                                plan=plan,
                                evidence=SubmissionEvidence(
                                    pre_submit_snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                                    final_snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                                    trace_path=str(trace_path) if trace_path.exists() else None,
                                    dom_snapshot_path=str(dom_snapshot_path) if dom_snapshot_path.exists() else None,
                                    post_submit_dom_snapshot_path=str(post_dom_snapshot_path) if post_dom_snapshot_path.exists() else None,
                                    field_audit=field_audit,
                                    network_errors=network_errors,
                                    failure_reason=failure_reason,
                                    final_url=page.url,
                                    missing_required_controls=missing_required_controls,
                                    submit_button_present=submit_count > 0,
                                    submit_button_enabled=submit_enabled,
                                ),
                            )
                        ), browser_left_open=browser_left_open)

                    if preview_only:
                        if self.capture_policy.dom_snapshots != CaptureMode.OFF:
                            post_dom_snapshot_path.write_text(await page.content(), encoding="utf-8")
                        if self.capture_policy.screenshots != CaptureMode.OFF:
                            await page.screenshot(path=str(final_snapshot_path), full_page=True)
                        if tracing_started:
                            await context.tracing.stop(path=str(trace_path))
                            tracing_started = False
                        return self._mark_browser_left_open(self._apply_capture_policy(
                            SubmissionResult(
                                status=JobLifecycleStatus.READY_FOR_REVIEW,
                                submitted=False,
                                uncertain=False,
                                message="Preview ready; submit not clicked",
                                snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                                trace_path=str(trace_path) if trace_path.exists() else None,
                                plan=plan,
                                evidence=SubmissionEvidence(
                                    pre_submit_snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                                    final_snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                                    trace_path=str(trace_path) if trace_path.exists() else None,
                                    dom_snapshot_path=str(dom_snapshot_path) if dom_snapshot_path.exists() else None,
                                    post_submit_dom_snapshot_path=str(post_dom_snapshot_path) if post_dom_snapshot_path.exists() else None,
                                    field_audit=field_audit,
                                    network_errors=network_errors,
                                    failure_reason=None,
                                    final_url=page.url,
                                    submit_button_present=True,
                                    submit_button_enabled=True,
                                    confirmation_strategy="pre_submit_preview",
                                ),
                            )
                        ), browser_left_open=browser_left_open)

                    submit = page.locator(submit_selector)
                    await submit.first.click()
                    await _sleep_ms(2500)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        await _sleep_ms(1000)
                    if self.capture_policy.screenshots != CaptureMode.OFF:
                        await page.screenshot(path=str(final_snapshot_path), full_page=True)
                    if self.capture_policy.dom_snapshots != CaptureMode.OFF:
                        post_dom_snapshot_path.write_text(await page.content(), encoding="utf-8")
                    if tracing_started:
                        await context.tracing.stop(path=str(trace_path))
                        tracing_started = False
                    confirmation_text = await self._confirmation_text(page)
                    submitted = bool(confirmation_text)
                    return self._mark_browser_left_open(self._apply_capture_policy(
                        SubmissionResult(
                            status=JobLifecycleStatus.SUBMITTED if submitted else JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                            submitted=submitted,
                            uncertain=not submitted,
                            message="Submitted" if submitted else "Submission outcome could not be confirmed",
                            snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                            trace_path=str(trace_path) if trace_path.exists() else None,
                            plan=plan,
                            evidence=SubmissionEvidence(
                                pre_submit_snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                                final_snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                                trace_path=str(trace_path) if trace_path.exists() else None,
                                dom_snapshot_path=str(dom_snapshot_path) if dom_snapshot_path.exists() else None,
                                post_submit_dom_snapshot_path=str(post_dom_snapshot_path) if post_dom_snapshot_path.exists() else None,
                                confirmation_text=confirmation_text,
                                confirmation_strategy="generic_text_match" if confirmation_text else None,
                                field_audit=field_audit,
                                network_errors=network_errors,
                                failure_reason=None if submitted else "confirmation_not_detected",
                                final_url=page.url,
                                submit_button_present=True,
                                submit_button_enabled=True,
                            ),
                        )
                    ), browser_left_open=browser_left_open)
                finally:
                    if page is not None and _listener_map:
                        self._remove_page_listeners(page, _listener_map)
                    try:
                        if tracing_started:
                            await self._await_with_timeout(context.tracing.stop(path=str(trace_path)), 10)
                    except Exception as exc:
                        log.debug("    [browser] Tracing stop error during cleanup: %s", exc)
                    if keep_browser_open and preview_only and session is not None and session.get("attached"):
                        log.info("    [browser] Leaving preview tab open for manual completion.")
                    elif keep_browser_open and preview_only:
                        network_errors.append("manual_handoff_unavailable:attached_browser_required")
                        log.warning("    [browser] keep_browser_open requested but the preview session is not attached; closing browser instead.")
                        try:
                            if session is not None:
                                await self._await_with_timeout(self._close_browser_session(session), 10)
                        except Exception as exc:
                            log.debug("    [browser] Session close error during cleanup: %s", exc)
                    else:
                        try:
                            if session is not None:
                                await self._await_with_timeout(self._close_browser_session(session), 10)
                        except Exception as exc:
                            log.debug("    [browser] Session close error during cleanup: %s", exc)
            finally:
                await self._stop_playwright_runtime(playwright)
                self._restore_playwright_exception_guard(loop, previous_handler)
        except PlaywrightTimeoutError as exc:
            return self._apply_capture_policy(SubmissionResult(
                status=JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                submitted=False,
                uncertain=True,
                message=str(exc),
                snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                trace_path=str(trace_path) if trace_path.exists() else None,
                plan=plan,
                evidence=SubmissionEvidence(
                    pre_submit_snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                    final_snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                    trace_path=str(trace_path) if trace_path.exists() else None,
                    dom_snapshot_path=str(dom_snapshot_path) if dom_snapshot_path.exists() else None,
                    post_submit_dom_snapshot_path=str(post_dom_snapshot_path) if post_dom_snapshot_path.exists() else None,
                    field_audit=field_audit,
                    network_errors=network_errors,
                    failure_reason="playwright_timeout",
                ),
            ))
        except RuntimeError as exc:
            message = str(exc)
            failure_reason = "playwright_runtime_blocked" if "could not start" in message.lower() else "submit_exception"
            return self._apply_capture_policy(SubmissionResult(
                status=JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                submitted=False,
                uncertain=True,
                message=message,
                snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                trace_path=str(trace_path) if trace_path.exists() else None,
                plan=plan,
                evidence=SubmissionEvidence(
                    pre_submit_snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                    final_snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                    trace_path=str(trace_path) if trace_path.exists() else None,
                    dom_snapshot_path=str(dom_snapshot_path) if dom_snapshot_path.exists() else None,
                    post_submit_dom_snapshot_path=str(post_dom_snapshot_path) if post_dom_snapshot_path.exists() else None,
                    field_audit=field_audit,
                    network_errors=network_errors,
                    failure_reason=failure_reason,
                ),
            ))
        except Exception as exc:
            return self._apply_capture_policy(SubmissionResult(
                status=JobLifecycleStatus.SUBMISSION_UNCERTAIN,
                submitted=False,
                uncertain=True,
                message=str(exc),
                snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                trace_path=str(trace_path) if trace_path.exists() else None,
                plan=plan,
                evidence=SubmissionEvidence(
                    pre_submit_snapshot_path=str(pre_submit_path) if pre_submit_path.exists() else None,
                    final_snapshot_path=str(final_snapshot_path) if final_snapshot_path.exists() else None,
                    trace_path=str(trace_path) if trace_path.exists() else None,
                    dom_snapshot_path=str(dom_snapshot_path) if dom_snapshot_path.exists() else None,
                    post_submit_dom_snapshot_path=str(post_dom_snapshot_path) if post_dom_snapshot_path.exists() else None,
                    field_audit=field_audit,
                    network_errors=network_errors,
                    failure_reason="submit_exception",
                ),
            ))

    def _choice_candidates(self, binding: FormFieldBinding) -> list[dict[str, str]]:
        option_details = list(binding.metadata.get("option_details") or [])
        raw_values = self._dedupe_strings([*binding.option_values, binding.option_value, *binding.values, binding.value])
        resolved: list[dict[str, str]] = []
        for raw in raw_values:
            match = None
            normalized_raw = self._normalize_choice(raw)
            for option in option_details:
                label = str(option.get("label") or "")
                value = str(option.get("value") or option.get("id") or label)
                if normalized_raw in {self._normalize_choice(label), self._normalize_choice(value)}:
                    match = {"label": label or raw, "value": value or raw}
                    break
            if match is None:
                match = {"label": raw, "value": raw}
            if match not in resolved:
                resolved.append(match)
        if not resolved and _is_demographic_binding(binding):
            fallback = _preferred_decline_option(option_details)
            if fallback is not None:
                resolved.append(fallback)
        return resolved

    def _name_candidates(self, binding: FormFieldBinding) -> list[str]:
        submission_binding = dict(binding.metadata.get("submission_binding") or {})
        names = [binding.source_field_name, submission_binding.get("name"), submission_binding.get("id")]
        if binding.source_field_name and not binding.source_field_name.startswith("job_application["):
            names.append(f"job_application[{binding.source_field_name}]")
        binding_name = str(submission_binding.get("name") or "").strip()
        if binding_name and not binding_name.startswith("job_application["):
            names.append(f"job_application[{binding_name}]")
        return self._dedupe_strings(names)

    @staticmethod
    def _preferred_location_search_value(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return text
        city = text.split(",", 1)[0].strip()
        return city or text

    def _is_location_field(self, binding: FormFieldBinding) -> bool:
        if str(binding.metadata.get("section") or "").lower() == "location":
            return True
        lowered = f"{binding.prompt_text} {binding.source_field_name}".lower()
        return "location" in lowered

    @staticmethod
    def _dedupe_strings(values: list[str | None]) -> list[str]:
        seen: list[str] = []
        for value in values:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @staticmethod
    def _normalize_choice(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _css_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    async def _safe_get_attribute(locator: Any, name: str) -> str | None:
        try:
            return await locator.get_attribute(name)
        except Exception:
            return None

    async def _apply_generic_binding(self, page, binding: FormFieldBinding) -> dict[str, Any]:
        audit = {
            "field": binding.source_field_name,
            "prompt": binding.prompt_text,
            "widget_type": binding.widget_type,
            "required": binding.required,
            "status": "skipped",
        }
        try:
            if binding.artifact_binding is not None:
                ok = await self._set_file(page, binding)
            elif self._is_date_binding(binding):
                ok = await self._fill_date(page, binding)
            elif binding.widget_type in {"select", "dropdown"}:
                ok = await self._select_option(page, binding)
            elif binding.widget_type in {"checkbox", "checkbox_group"}:
                ok = await self._set_checkboxes(page, binding)
            elif binding.widget_type in {"radio", "radio_group"}:
                ok = await self._set_radios(page, binding)
            else:
                ok = await self._fill_text(page, binding)
            audit["status"] = "bound" if ok else "missing"
        except Exception as exc:
            log.warning("    [form] Generic binding error for field %s: %s: %s", binding.source_field_name, type(exc).__name__, exc)
            audit["status"] = "error"
            audit["error"] = str(exc)
            audit["error_type"] = type(exc).__name__
        return audit

    async def _second_pass_generic_required_fields(self, page, plan: SubmissionPlan, field_audit: list[dict[str, Any]]) -> None:
        await _sleep_ms(500)
        selectors = "input:visible[required], select:visible[required], textarea:visible[required]"
        try:
            locators = await page.locator(selectors).all()
        except Exception as exc:
            log.debug("    [form] Generic second-pass locator collection failed: %s", exc)
            return
        for locator in locators:
            try:
                descriptor = await _locator_descriptor(locator)
                if not descriptor or _descriptor_has_value(descriptor):
                    continue
                binding = self._binding_for_descriptor(plan, descriptor)
                if binding is None:
                    continue
                _merge_field_audit_entry(field_audit, await self._apply_generic_binding(page, binding), second_pass=True)
            except Exception as exc:
                log.debug("    [form] Generic second-pass retry failed: %s", exc)

    def _binding_for_descriptor(self, plan: SubmissionPlan, descriptor: dict[str, Any]) -> FormFieldBinding | None:
        for binding in plan.fields:
            if _descriptor_matches_binding(descriptor, binding):
                return binding
        return None

    @staticmethod
    def _is_date_binding(binding: FormFieldBinding) -> bool:
        widget_type = str(binding.widget_type or "").strip().casefold()
        field_type = str(binding.metadata.get("type") or binding.metadata.get("input_type") or "").strip().casefold()
        return widget_type in {"date", "input_date"} or field_type == "date"

    async def _fill_date(self, page, binding: FormFieldBinding) -> bool:
        locator = await self._field_locator(page, binding)
        if locator is None:
            return False
        raw_values = [binding.value, *binding.values]
        normalized = next((candidate for candidate in (_normalize_date_candidate(value) for value in raw_values) if candidate), None)
        if normalized is None:
            prompt = f"{binding.prompt_text} {binding.source_field_name}".casefold()
            if any(token in prompt for token in ("start date", "availability", "earliest", "when can you", "graduation")):
                normalized = (date.today() + timedelta(days=14)).isoformat()
        if normalized is None:
            return False
        await locator.fill(normalized)
        return True

    async def _field_locator(self, page, binding: FormFieldBinding):
        label = binding.prompt_text.strip()
        if label:
            locator = page.get_by_label(label, exact=False)
            if await locator.count() > 0:
                return locator.first
        for name in self._name_candidates(binding):
            locator = page.locator(f"[name={self._css_string(name)}]")
            if await locator.count() > 0:
                return locator.first
            locator = page.locator(f"[id={self._css_string(name)}]")
            if await locator.count() > 0:
                return locator.first
        if self._is_location_field(binding):
            for selector in (
                "input[name*='location']",
                "input[id*='location']",
                "input[autocomplete='address-level2']",
                "input[placeholder*='Location']",
                "input[aria-label*='Location']",
            ):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    return locator.first
        return None

    async def _fill_text(self, page, binding: FormFieldBinding) -> bool:
        locator = await self._field_locator(page, binding)
        if locator is None:
            return False
        value = binding.value or (binding.values[0] if binding.values else "")
        if not value:
            return False
        search_value = self._preferred_location_search_value(value) if self._is_location_field(binding) else value
        await locator.fill(search_value)
        if await self._locator_has_autocomplete(locator):
            candidates = self._dedupe_strings([search_value, value, search_value.split(",")[0] if search_value else None, value.split(",")[0] if value else None])
            if await self._select_combobox_choice(binding, locator, candidates):
                return True
        return True

    async def _select_option(self, page, binding: FormFieldBinding) -> bool:
        locator = await self._field_locator(page, binding)
        if locator is None:
            return False
        choices = self._choice_candidates(binding)
        values = [choice["value"] for choice in choices if choice["value"]]
        labels = [choice["label"] for choice in choices if choice["label"]]
        raw_type = str((binding.metadata.get("submission_binding") or {}).get("raw_type") or "").strip().lower()
        role = (await self._safe_get_attribute(locator, "role") or "").strip().lower()
        aria_autocomplete = (await self._safe_get_attribute(locator, "aria-autocomplete") or "").strip().lower()
        if raw_type == "multi_value_single_select" or role == "combobox" or aria_autocomplete == "list":
            return await self._select_combobox_choice(binding, locator, self._dedupe_strings([*labels, *values]))
        if values:
            try:
                payload: str | list[str] = values[0] if len(values) == 1 else values
                await locator.select_option(value=payload)
                return True
            except Exception as exc:
                log.debug("    [form] generic select_option by value failed for %s: %s", binding.source_field_name, exc)
        if labels:
            try:
                payload: str | list[str] = labels[0] if len(labels) == 1 else labels
                await locator.select_option(label=payload)
                return True
            except Exception as exc:
                log.debug("    [form] generic select_option by label failed for %s: %s", binding.source_field_name, exc)
        return await self._select_combobox_choice(binding, locator, self._dedupe_strings([*labels, *values]))

    async def _set_checkboxes(self, page, binding: FormFieldBinding) -> bool:
        choices = self._choice_candidates(binding)
        bound = False
        for choice in choices:
            locator = await self._choice_input_locator(page, binding, choice, "checkbox")
            if locator is None:
                continue
            await locator.check()
            bound = True
        return bound

    async def _set_radios(self, page, binding: FormFieldBinding) -> bool:
        choices = self._choice_candidates(binding)
        if not choices:
            return False
        locator = await self._choice_input_locator(page, binding, choices[0], "radio")
        if locator is None:
            return False
        await locator.check()
        return True

    async def _choice_input_locator(self, page, binding: FormFieldBinding, choice: dict[str, str], input_type: str):
        for name in self._name_candidates(binding):
            for candidate in self._dedupe_strings([choice["value"], choice["label"]]):
                locator = page.locator(
                    f"input[type='{input_type}'][name={self._css_string(name)}][value={self._css_string(candidate)}]"
                )
                if await locator.count() > 0:
                    return locator.first
        for candidate in self._dedupe_strings([choice["value"], choice["label"]]):
            locator = page.locator(f"input[type='{input_type}'][value={self._css_string(candidate)}]")
            if await locator.count() > 0:
                return locator.first
        label = choice["label"]
        if label:
            locator = page.get_by_label(label, exact=False)
            if await locator.count() > 0:
                candidate_locator = locator.first
                locator_type = (await self._safe_get_attribute(candidate_locator, "type") or "").strip().lower()
                if locator_type == input_type:
                    return candidate_locator
        return None

    async def _set_file(self, page, binding: FormFieldBinding) -> bool:
        if binding.artifact_binding is None:
            return False
        for name in self._name_candidates(binding):
            locator = page.locator(f"input[type='file'][name={self._css_string(name)}]")
            if await locator.count() > 0:
                await locator.first.set_input_files(binding.artifact_binding.path)
                return True
            locator = page.locator(f"input[type='file'][id={self._css_string(name)}]")
            if await locator.count() > 0:
                await locator.first.set_input_files(binding.artifact_binding.path)
                return True
        if binding.prompt_text:
            locator = page.get_by_label(binding.prompt_text, exact=False)
            if await locator.count() > 0:
                await locator.first.set_input_files(binding.artifact_binding.path)
                return True
        lowered = f"{binding.prompt_text} {binding.source_field_name}".lower()
        if "resume" in lowered:
            locator = page.locator("input[type='file'][name*='resume'], input[type='file'][id*='resume']")
            if await locator.count() > 0:
                await locator.first.set_input_files(binding.artifact_binding.path)
                return True
        if "cover" in lowered:
            locator = page.locator("input[type='file'][name*='cover'], input[type='file'][id*='cover']")
            if await locator.count() > 0:
                await locator.first.set_input_files(binding.artifact_binding.path)
                return True
        locator = page.locator("input[type='file']")
        if await locator.count() == 0:
            return False
        await locator.first.set_input_files(binding.artifact_binding.path)
        return True

    async def _greenhouse_confirmation(self, page) -> tuple[str | None, str | None]:
        text = await self._confirmation_text(page)
        if text:
            return text, "explicit_success_text"
        current_url = page.url.lower()
        if any(marker in current_url for marker in _GREENHOUSE_SUCCESS_URL_MARKERS):
            return page.url, "url_transition"
        return None, None

    async def _confirmation_text(self, page) -> str | None:
        for pattern in _GREENHOUSE_SUCCESS_TEXT_PATTERNS:
            locator = page.locator(pattern)
            if await locator.count() > 0:
                try:
                    text = await locator.first.inner_text()
                except Exception:
                    text = pattern
                return text
        return None


# ---------------------------------------------------------------------------
# Snapshot-based fallback and failure classification
# ---------------------------------------------------------------------------


def classify_submission_failure(evidence: SubmissionEvidence | None) -> dict[str, Any]:
    """Classify a submission failure from evidence into a structured reason.

    Returns a dict with:
      - failure_category: high-level category
      - failure_reason: specific reason string
      - retryable: whether the failure could succeed on retry
      - escalation_needed: whether human input is required
    """
    if evidence is None:
        return {
            "failure_category": "unknown",
            "failure_reason": "no_evidence",
            "retryable": False,
            "escalation_needed": True,
        }

    reason = evidence.failure_reason or ""
    lowered = reason.lower()

    # Login / account wall
    if "login" in lowered or "account_wall" in lowered:
        return {
            "failure_category": "access_blocked",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": True,
        }

    # Captcha
    if "captcha" in lowered or "antibot" in lowered:
        return {
            "failure_category": "captcha_blocked",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": True,
        }

    # Rate limiting
    if "429" in lowered or "rate_limit" in lowered or "rate limit" in lowered:
        return {
            "failure_category": "rate_limited",
            "failure_reason": reason,
            "retryable": True,
            "escalation_needed": False,
        }

    # Playwright unavailable
    if "playwright_unavailable" in lowered or "playwright_runtime_blocked" in lowered:
        return {
            "failure_category": "infrastructure",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": True,
        }

    # Timeout
    if "timeout" in lowered:
        return {
            "failure_category": "timeout",
            "failure_reason": reason,
            "retryable": True,
            "escalation_needed": False,
        }

    # Missing bindings
    if "missing_required" in lowered or "file_upload_failed" in lowered:
        return {
            "failure_category": "form_binding",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": True,
        }

    # Location autocomplete
    if "location_autocomplete" in lowered:
        return {
            "failure_category": "form_binding",
            "failure_reason": reason,
            "retryable": True,
            "escalation_needed": False,
        }

    # Validation errors
    if "validation" in lowered:
        return {
            "failure_category": "validation_error",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": True,
        }

    # Submit button missing/disabled
    if "submit_button" in lowered:
        return {
            "failure_category": "form_structure",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": True,
        }

    # Confirmation not detected (uncertain)
    if "confirmation_not_detected" in lowered:
        return {
            "failure_category": "uncertain",
            "failure_reason": reason,
            "retryable": False,
            "escalation_needed": False,
        }

    return {
        "failure_category": "unknown",
        "failure_reason": reason or "unclassified",
        "retryable": False,
        "escalation_needed": True,
    }


def analyze_dom_snapshot(dom_html: str) -> dict[str, Any]:
    """Analyze a DOM snapshot for login walls, captchas, and page state.

    This is a lightweight heuristic analysis without rendering.
    Returns structured findings about the page.
    """
    lowered = dom_html.lower() if dom_html else ""
    has_form = "<form" in lowered
    has_submit_button = any(
        marker in lowered
        for marker in (
            'type="submit"',
            "type='submit'",
            'id="submit_app"',
            "id='submit_app'",
            'id="btn-submit"',
            "id='btn-submit'",
            'data-qa="btn-submit"',
            "data-qa='btn-submit'",
            'data-testid="submit-application"',
            "data-testid='submit-application'",
            'ashby-application-form-submit-button',
            'submit application',
        )
    )
    findings: dict[str, Any] = {
        "has_login_wall": False,
        "has_captcha": False,
        "has_form": has_form,
        "has_submit_button": has_submit_button,
        "has_confirmation": any(
            marker in lowered
            for marker in ("thank you", "application received", "application submitted", "application-confirmation")
        ),
        "detected_issues": [],
    }

    for pattern in _LOGIN_WALL_PATTERNS:
        if pattern in lowered:
            findings["has_login_wall"] = True
            findings["detected_issues"].append(f"login_wall:{pattern}")
            break

    captcha_pattern = next((pattern for pattern in _CAPTCHA_PATTERNS if pattern in lowered), None)
    if captcha_pattern:
        findings["detected_issues"].append(f"captcha:{captcha_pattern}")
        passive_markers = (
            "grecaptcha-badge",
            "size=invisible",
            "recaptcha/enterprise.js?render=",
            "grecaptcha-logo",
        )
        widget_markers = (
            "g-recaptcha",
            "hcaptcha",
            "h-captcha",
            "data-sitekey",
            "recaptcha/api2",
            "cf-chl-widget",
        )
        blocker_markers = (
            "verify you are human",
            "complete the captcha",
            "captcha challenge",
            "i'm not a robot",
            "cf-chl-widget",
            "hcaptcha-box",
            "challenge-running",
            "bot challenge",
        )
        has_blocker = any(marker in lowered for marker in blocker_markers)
        has_passive = any(marker in lowered for marker in passive_markers)
        has_widget = any(marker in lowered for marker in widget_markers)
        # Treat captcha as passive (non-blocking) when no explicit blocker
        # challenge or explicit widget is present. During HTTP-only enrichment
        # the page may include passive badge/script markers without a rendered
        # challenge, but explicit widgets should still count as blocking.
        passive_only = not has_blocker and has_passive and not has_widget
        findings["has_captcha"] = not passive_only

    return findings
