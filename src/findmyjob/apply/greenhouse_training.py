"""My Greenhouse page model for the review-first training workflow."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

from findmyjob.core.types import TrainingPageCapture


VALID_POSTED_WINDOWS = (1, 5, 10, 30)
_DEFAULT_POSTED_WINDOW = 10
JS_VIEW_ACTION = "__JS_VIEW_BUTTON__"
JS_COMPANY_PAGE_ACTION = "__JS_COMPANY_BUTTON__"
JS_APPLY_ACTION = "__JS_APPLY_BUTTON__"

_PRIMARY_JOB_ROW_SELECTOR = "table tbody tr, [class*='job-row'], [class*='job_row'], [data-job-id]"
_FALLBACK_JOB_ROW_SELECTOR = "[class*='job'], [role='row'], li:has(a[href*='/job']), div:has(> a[href*='/job']), li:has(a[href*='/jobs']), div:has(> a[href*='/jobs'])"
_JOB_LINK_SELECTOR = "a[href*='/job'], a[href*='/jobs']"
_VIEW_LINK_SELECTORS = (
    "a:has-text('View')",
    _JOB_LINK_SELECTOR,
)
_VIEW_CLICK_SELECTORS = (
    "button:has-text('View')",
    "[role='button']:has-text('View')",
)
_COMPANY_PAGE_SELECTORS = (
    "a[href*='boards.greenhouse.io']",
    "a[href*='job-boards.greenhouse.io']",
    "a:has-text('View job post')",
    "a:has-text('View posting')",
    "a:has-text('Original posting')",
    "a[href*='/jobs/'][target='_blank']",
    "a[class*='external']",
)
_COMPANY_CLICK_SELECTORS = (
    "button:has-text('View job post')",
    "button:has-text('Original posting')",
    "[role='button']:has-text('View job post')",
)
_APPLY_LINK_SELECTORS = (
    "a[href*='/apply']",
    "a[href*='#application']",
    "a[href*='application_form']",
    "a:has-text('Apply')",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply Now')",
)
_APPLY_CLICK_SELECTORS = (
    "button:has-text('Apply')",
    "button:has-text('Apply for this job')",
    "button:has-text('Apply Now')",
    "[role='button']:has-text('Apply')",
    "input[type='submit'][value*='Apply']",
    "input[type='button'][value*='Apply']",
    "a[href='#']:has-text('Apply')",
    "a[onclick]:has-text('Apply')",
)


class GreenhousePageModelError(RuntimeError):
    """Raised for navigation or extraction failures on the My Greenhouse page."""


def _stable_page_id(page: "Page") -> str:
    return str(id(page))


def _current_url(page_or_url: "Page | str") -> str:
    return page_or_url if isinstance(page_or_url, str) else page_or_url.url


def _resolve_page_url(page_or_url: "Page | str", href: str | None) -> str | None:
    if not href:
        return None
    candidate = str(href).strip()
    if not candidate or candidate in {"#", "javascript:void(0)", "javascript:void(0);"}:
        return None
    if candidate.startswith("javascript:"):
        return None
    return urljoin(_current_url(page_or_url), candidate)


def _page_host(page: "Page") -> str:
    return urlsplit(page.url).netloc.casefold()


async def _job_rows(page: "Page"):
    rows = page.locator(_PRIMARY_JOB_ROW_SELECTOR)
    return rows if await _safe_count(rows) > 0 else page.locator(_FALLBACK_JOB_ROW_SELECTOR)


async def _safe_count(locator: "Locator") -> int:
    try:
        return await locator.count()
    except Exception:
        return 0


async def _find_first_locator(container: Any, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = container.locator(selector)
        if await _safe_count(locator) > 0:
            return locator.first
    return None


def _page_context(page: "Page"):
    context = getattr(page, "context", None)
    if callable(context):
        try:
            return context()
        except TypeError:
            return context
    return context


async def _wait_for_page(page: "Page") -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass


async def _click_and_follow(page: "Page", control) -> "Page":
    context = _page_context(page)
    existing_ids = {_stable_page_id(candidate) for candidate in getattr(context, "pages", [])} if context is not None else set()
    await control.click()
    try:
        await page.wait_for_timeout(250)
    except Exception:
        pass
    if context is not None:
        for candidate in list(getattr(context, "pages", [])):
            if _stable_page_id(candidate) in existing_ids:
                continue
            await _wait_for_page(candidate)
            try:
                await candidate.bring_to_front()
            except Exception:
                pass
            return candidate
    await _wait_for_page(page)
    return page


def _split_row_text(text: str, title_text: str | None) -> list[str]:
    parts = [part.strip() for part in str(text or "").replace("\t", "\n").splitlines() if part.strip()]
    if title_text and parts and parts[0].casefold() == title_text.strip().casefold():
        return parts[1:]
    return parts


# ---------------------------------------------------------------------------
# Filter manipulation
# ---------------------------------------------------------------------------


async def set_posted_window(page: "Page", days: int) -> None:
    if days not in VALID_POSTED_WINDOWS:
        raise ValueError(f"posted_window must be one of {VALID_POSTED_WINDOWS}, got {days}")

    select_locator = page.locator("select").filter(has_text="day")
    if await _safe_count(select_locator) > 0:
        await select_locator.first.select_option(str(days))
        await page.wait_for_timeout(500)
        return

    dropdown_trigger = page.locator(
        "[data-provides='filter'], [aria-label*='posted'], [class*='date-filter'], [class*='posted-filter'], button:has-text('Posted'), button:has-text('Day')"
    )
    if await _safe_count(dropdown_trigger) > 0:
        await dropdown_trigger.first.click()
        await page.wait_for_timeout(250)
        option = page.locator(f"text=/{days}\\s*day/i")
        if await _safe_count(option) > 0:
            await option.first.click()
            await page.wait_for_timeout(500)
            return
        await page.keyboard.press("Escape")

    base = page.url.split("?")[0]
    await page.goto(f"{base}?posted_within={days}", wait_until="domcontentloaded")
    await _wait_for_page(page)


# ---------------------------------------------------------------------------
# Job list harvesting
# ---------------------------------------------------------------------------


async def harvest_visible_jobs(page: "Page", max_jobs: int = 50) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    rows = await _job_rows(page)
    count = await _safe_count(rows)
    for index in range(min(count, max_jobs)):
        row = rows.nth(index)
        try:
            primary_link = row.locator(_JOB_LINK_SELECTOR).first
            href = await primary_link.get_attribute("href") if await _safe_count(row.locator(_JOB_LINK_SELECTOR)) > 0 else None
            title_text = (await primary_link.inner_text()).strip() if href else None
            resolved_url = _resolve_page_url(page, href)
            row_text = (await row.inner_text()).strip()
            remaining = _split_row_text(row_text, title_text)
            if title_text:
                parsed_title = title_text
                company = remaining[0] if len(remaining) >= 1 else None
                location = remaining[1] if len(remaining) >= 2 else None
                posted_text = remaining[-1] if len(remaining) >= 3 else None
            else:
                parsed_title = remaining[0] if len(remaining) >= 1 else ""
                company = remaining[1] if len(remaining) >= 2 else None
                location = remaining[2] if len(remaining) >= 3 else None
                posted_text = remaining[-1] if len(remaining) >= 4 else None
            if not resolved_url:
                view_link = await _find_first_locator(row, _VIEW_LINK_SELECTORS)
                if view_link is not None:
                    resolved_url = _resolve_page_url(page, await view_link.get_attribute("href"))
            view_action = None
            if not resolved_url:
                view_control = await _find_first_locator(row, _VIEW_CLICK_SELECTORS)
                if view_control is not None:
                    view_action = JS_VIEW_ACTION
            jobs.append(
                {
                    "url": resolved_url or "",
                    "title": parsed_title or "",
                    "company": company,
                    "location": location,
                    "posted_text": posted_text,
                    "row_text": row_text[:500],
                    "view_action": view_action,
                }
            )
        except Exception:
            continue
    return jobs


# ---------------------------------------------------------------------------
# Job detail & apply navigation
# ---------------------------------------------------------------------------


async def _find_matching_job_row(page: "Page", job_ref: dict[str, Any]):
    rows = await _job_rows(page)
    count = await _safe_count(rows)
    expected_title = str(job_ref.get("title") or "").strip().casefold()
    expected_company = str(job_ref.get("company") or "").strip().casefold()
    expected_row_text = str(job_ref.get("row_text") or "").strip().casefold()
    for index in range(count):
        row = rows.nth(index)
        text = str(await row.inner_text()).strip().casefold()
        title_match = not expected_title or expected_title in text
        company_match = not expected_company or expected_company in text
        row_match = not expected_row_text or expected_row_text in text
        if title_match and company_match and row_match:
            return row
    raise GreenhousePageModelError("Could not relocate the selected job row on the My Greenhouse jobs list.")


async def open_job_view(page: "Page", job_ref: str | dict[str, Any]) -> tuple["Page", TrainingPageCapture]:
    notes: list[str] = []
    target_page = page
    if isinstance(job_ref, str):
        resolved_url = _resolve_page_url(page, job_ref) or job_ref
        await page.goto(resolved_url, wait_until="domcontentloaded")
        await _wait_for_page(page)
    else:
        direct_url = _resolve_page_url(page, str(job_ref.get("url") or ""))
        if direct_url:
            await page.goto(direct_url, wait_until="domcontentloaded")
            await _wait_for_page(page)
        else:
            row = await _find_matching_job_row(page, job_ref)
            control = await _find_first_locator(row, _VIEW_CLICK_SELECTORS)
            if control is None:
                raise GreenhousePageModelError("Could not find a View control for the selected job.")
            target_page = await _click_and_follow(page, control)
            notes.append("Opened job view via click control.")
    return target_page, TrainingPageCapture(stage="job_view", url=target_page.url, page_title=await target_page.title(), layout_notes=notes)


async def navigate_to_job_view(page: "Page", job_ref: str | dict[str, Any]) -> TrainingPageCapture:
    _page, capture = await open_job_view(page, job_ref)
    return capture


async def find_company_job_page_url(page: "Page") -> str | None:
    for selector in _COMPANY_PAGE_SELECTORS:
        locator = page.locator(selector)
        if await _safe_count(locator) == 0:
            continue
        resolved = _resolve_page_url(page, await locator.first.get_attribute("href"))
        if resolved:
            return resolved
    for selector in _COMPANY_CLICK_SELECTORS:
        locator = page.locator(selector)
        if await _safe_count(locator) > 0:
            return JS_COMPANY_PAGE_ACTION
    return None


async def open_company_job_page(page: "Page", company_target: str) -> tuple["Page", TrainingPageCapture]:
    notes: list[str] = []
    target_page = page
    if company_target == JS_COMPANY_PAGE_ACTION:
        control = await _find_first_locator(page, _COMPANY_CLICK_SELECTORS)
        if control is None:
            raise GreenhousePageModelError("Could not locate a control for the company job page.")
        target_page = await _click_and_follow(page, control)
        notes.append("Opened company page via click control.")
    else:
        resolved_url = _resolve_page_url(page, company_target)
        if not resolved_url:
            raise GreenhousePageModelError("Could not resolve the company job page URL.")
        await page.goto(resolved_url, wait_until="domcontentloaded")
        await _wait_for_page(page)
    return target_page, TrainingPageCapture(stage="company_page", url=target_page.url, page_title=await target_page.title(), layout_notes=notes)


async def navigate_to_company_job_page(page: "Page", company_target: str) -> TrainingPageCapture:
    _page, capture = await open_company_job_page(page, company_target)
    return capture


async def find_apply_url(page: "Page") -> str | None:
    for selector in _APPLY_LINK_SELECTORS:
        locator = page.locator(selector)
        if await _safe_count(locator) == 0:
            continue
        resolved = _resolve_page_url(page, await locator.first.get_attribute("href"))
        if resolved:
            return resolved
        return JS_APPLY_ACTION
    for selector in _APPLY_CLICK_SELECTORS:
        locator = page.locator(selector)
        if await _safe_count(locator) > 0:
            return JS_APPLY_ACTION
    return None


async def open_apply_page(page: "Page", apply_target: str) -> tuple["Page", TrainingPageCapture]:
    notes: list[str] = []
    target_page = page
    if apply_target == JS_APPLY_ACTION:
        control = await _find_first_locator(page, _APPLY_CLICK_SELECTORS)
        if control is None:
            raise GreenhousePageModelError("Could not locate an Apply control to click.")
        target_page = await _click_and_follow(page, control)
        notes.append("Opened apply page via click control.")
    else:
        resolved_url = _resolve_page_url(page, apply_target)
        if not resolved_url:
            raise GreenhousePageModelError("Could not resolve the Apply URL.")
        await page.goto(resolved_url, wait_until="domcontentloaded")
        await _wait_for_page(page)
    return target_page, TrainingPageCapture(stage="apply_page", url=target_page.url, page_title=await target_page.title(), layout_notes=notes)


async def navigate_to_apply(page: "Page", apply_target: str) -> TrainingPageCapture:
    _page, capture = await open_apply_page(page, apply_target)
    return capture


# ---------------------------------------------------------------------------
# Form field extraction
# ---------------------------------------------------------------------------


async def extract_form_fields(page: "Page") -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    inputs = page.locator(
        "input:not([type='hidden']):not([type='submit']), textarea, select, input[type='file']"
    )
    count = await _safe_count(inputs)
    for index in range(count):
        field = inputs.nth(index)
        try:
            name = await field.get_attribute("name") or ""
            input_id = await field.get_attribute("id") or ""
            tag_name = await field.evaluate("el => el.tagName.toLowerCase()")
            input_type = await field.get_attribute("type") or str(tag_name or "input")
            if str(tag_name).lower() == "select":
                input_type = "select"
            elif str(tag_name).lower() == "textarea":
                input_type = "textarea"
            required = (await field.get_attribute("required") is not None) or (await field.get_attribute("aria-required") == "true")
            label_text = ""
            if input_id:
                label = page.locator(f"label[for='{input_id}']")
                if await _safe_count(label) > 0:
                    label_text = (await label.first.inner_text()).strip()
            options: list[str] = []
            if input_type == "select":
                option_elements = field.locator("option")
                for option_index in range(await _safe_count(option_elements)):
                    option_text = (await option_elements.nth(option_index).inner_text()).strip()
                    if option_text:
                        options.append(option_text)
            fields.append(
                {
                    "name": name,
                    "label": label_text,
                    "type": input_type,
                    "required": required,
                    "options": options,
                    "id": input_id,
                }
            )
        except Exception:
            continue
    return fields


# ---------------------------------------------------------------------------
# Page capture helpers
# ---------------------------------------------------------------------------


async def capture_screenshot(page: "Page", output_dir: Path, prefix: str = "training") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{prefix}_{ts}_{uuid4().hex[:8]}.png"
    await page.screenshot(path=str(path), full_page=True)
    return path


async def capture_dom_snapshot(page: "Page", output_dir: Path, prefix: str = "training") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{prefix}_{ts}_{uuid4().hex[:8]}.html"
    path.write_text(await page.content(), encoding="utf-8")
    return path


async def extract_job_description(page: "Page") -> str:
    selectors = [
        "[class*='job-description']",
        "[class*='job_description']",
        "[class*='description']",
        "[id*='description']",
        "[data-qa='job-description']",
        "article",
        ".content",
        "main",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if await _safe_count(locator) == 0:
            continue
        text = (await locator.first.inner_text()).strip()
        if len(text) > 50:
            return text
    body = page.locator("body")
    if await _safe_count(body) > 0:
        return (await body.inner_text()).strip()[:5000]
    return ""


async def inspect_training_job_path(
    page: "Page",
    job_ref: str | dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    layout_notes: list[str] = []
    captures: list[TrainingPageCapture] = []
    dom_snapshot_paths: list[str] = []

    current_page, view_capture = await open_job_view(page, job_ref)
    view_capture.screenshot_path = str(await capture_screenshot(current_page, output_dir, prefix="job_view"))
    view_capture.dom_snapshot_path = str(await capture_dom_snapshot(current_page, output_dir, prefix="job_view_dom"))
    view_capture.job_description_text = await extract_job_description(current_page)
    captures.append(view_capture)
    dom_snapshot_paths.append(view_capture.dom_snapshot_path)

    company_page_url: str | None = None
    apply_page_url: str | None = None
    form_fields: list[dict[str, Any]] = []
    best_description = view_capture.job_description_text or ""

    company_target = await find_company_job_page_url(current_page)
    if company_target:
        try:
            current_page, company_capture = await open_company_job_page(current_page, company_target)
            company_capture.screenshot_path = str(await capture_screenshot(current_page, output_dir, prefix="company_job"))
            company_capture.dom_snapshot_path = str(await capture_dom_snapshot(current_page, output_dir, prefix="company_job_dom"))
            company_capture.job_description_text = await extract_job_description(current_page)
            company_page_url = company_capture.url
            captures.append(company_capture)
            dom_snapshot_paths.append(company_capture.dom_snapshot_path)
            if len((company_capture.job_description_text or "")) >= len(best_description):
                best_description = company_capture.job_description_text or best_description
        except Exception as exc:
            layout_notes.append(f"Company page navigation error: {exc}")
    elif _page_host(current_page) != "my.greenhouse.io":
        company_page_url = current_page.url
        layout_notes.append("Job view already resolved to the company page.")
    else:
        layout_notes.append("No company job page found on the job view page.")

    apply_target = await find_apply_url(current_page)
    if apply_target:
        try:
            current_page, apply_capture = await open_apply_page(current_page, apply_target)
            apply_capture.screenshot_path = str(await capture_screenshot(current_page, output_dir, prefix="apply_form"))
            apply_capture.dom_snapshot_path = str(await capture_dom_snapshot(current_page, output_dir, prefix="apply_dom"))
            apply_capture.job_description_text = await extract_job_description(current_page)
            form_fields = await extract_form_fields(current_page)
            apply_capture.extracted_fields = list(form_fields)
            apply_page_url = apply_capture.url
            captures.append(apply_capture)
            dom_snapshot_paths.append(apply_capture.dom_snapshot_path)
            if len((apply_capture.job_description_text or "")) >= len(best_description):
                best_description = apply_capture.job_description_text or best_description
            if apply_target == JS_APPLY_ACTION:
                layout_notes.append("Apply form opened via click control.")
        except Exception as exc:
            layout_notes.append(f"Apply page navigation error: {exc}")
    else:
        existing_fields = await extract_form_fields(current_page)
        if existing_fields:
            apply_page_url = current_page.url
            form_fields = existing_fields
            layout_notes.append("Current page already exposed the application form.")
        else:
            layout_notes.append("No apply control found on the current page.")

    for capture in captures:
        for note in capture.layout_notes:
            if note not in layout_notes:
                layout_notes.append(note)

    return {
        "final_page": current_page,
        "page_captures": captures,
        "company_page_url": company_page_url,
        "apply_url": apply_page_url,
        "job_description_text": (best_description or "").strip(),
        "form_fields": form_fields,
        "screenshot_paths": [capture.screenshot_path for capture in captures if capture.screenshot_path],
        "dom_snapshot_paths": [path for path in dom_snapshot_paths if path],
        "layout_notes": layout_notes,
    }



