from __future__ import annotations

from pathlib import Path
import pytest

from findmyjob.apply.browser import _GENERIC_SUBMIT_SELECTOR, _GREENHOUSE_SUBMIT_SELECTOR, GreenhouseArtifactPaths, GreenhouseBindingError, GreenhouseHostedFormFlow, PlaywrightSubmitter, analyze_dom_snapshot
from findmyjob.apply.forms import extract_questions_from_html
from findmyjob.core.enums import ArtifactKind, JobLifecycleStatus
from findmyjob.core.types import ArtifactBinding, FormFieldBinding, SubmissionCapturePolicy, SubmissionPlan, SubmissionResult


class FakeLocator:
    def __init__(self, count: int = 0, text: str | None = None, attributes: dict[str, str] | None = None) -> None:
        self._count = count
        self._text = text or ""
        self.attributes = attributes or {}
        self.selected: list[tuple[str, object]] = []
        self.files: list[str] = []
        self.checked = False
        self.filled: list[str] = []
        self.clicks = 0
        self.pressed: list[str] = []
        self.blurred = False

    def nth(self, index: int) -> FakeLocator:
        # For simplicity, return self; the test only needs to iterate and check inner_text
        return self

    async def count(self) -> int:
        return self._count

    async def all(self):
        return []

    @property
    def first(self):
        return self

    async def select_option(self, **kwargs):
        # Simulate Playwright: select_option only works on native <select> elements.
        if self.attributes.get('role') == 'combobox' or self.attributes.get('aria-autocomplete') == 'list':
            raise Exception("Element is not a select element")
        if self._count == 0:
            raise RuntimeError("missing")
        self.selected.append((next(iter(kwargs.keys())), next(iter(kwargs.values()))))

    async def set_input_files(self, path: str) -> None:
        self.files.append(path)

    async def check(self) -> None:
        self.checked = True

    async def inner_text(self) -> str:
        return self._text

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def click(self) -> None:
        self.clicks += 1

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, script: str):
        if "clearButton.click" in script:
            self.filled = []
            self.selected = []
            self.pressed = []
            self.attributes["has-selection"] = "false"
            self.attributes["aria-invalid"] = "false"
            return None
        if "typeof el.blur" in script:
            self.blurred = True
            if self.attributes.get("auto-select-on-blur") == "true":
                self.attributes["has-selection"] = "true"
            return None
        if "select__single-value" in script or "has-value" in script:
            return self.attributes.get("has-selection") == "true"
        return None


class FakePage:
    def __init__(
        self,
        selectors: dict[str, FakeLocator] | None = None,
        labels: dict[str, FakeLocator] | None = None,
        evaluate_result: dict | None = None,
        url: str = "https://boards.greenhouse.io/acme/jobs/123",
        content_html: str = "<html></html>",
    ) -> None:
        self.selectors = selectors or {}
        self.labels = labels or {}
        self.evaluate_result = evaluate_result or {}
        self.url = url
        self.content_html = content_html
        self.listeners: dict[str, object] = {}
        self.default_timeout = None

    def locator(self, selector: str) -> FakeLocator:
        return self.selectors.get(selector, FakeLocator())

    def get_by_label(self, label: str, exact: bool = False) -> FakeLocator:
        return self.labels.get(label, FakeLocator())

    async def evaluate(self, script: str):
        return self.evaluate_result

    async def content(self) -> str:
        return self.content_html

    async def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_text("image", encoding="utf-8")

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        return None

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        return None

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.default_timeout = timeout_ms

    def on(self, event: str, listener) -> None:
        self.listeners[event] = listener

    def remove_listener(self, event: str, listener) -> None:
        if self.listeners.get(event) == listener:
            del self.listeners[event]


class FakeTracing:
    async def start(self, screenshots: bool = True, snapshots: bool = True) -> None:
        _ = (screenshots, snapshots)
        return None

    async def stop(self, path: str) -> None:
        Path(path).write_text('trace', encoding='utf-8')


class FakeContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()


def build_flow(page: FakePage, tmp_path: Path) -> GreenhouseHostedFormFlow:
    artifacts = GreenhouseArtifactPaths(
        pre_submit_snapshot_path=tmp_path / "pre-submit.png",
        final_snapshot_path=tmp_path / "submit-final.png",
        trace_path=tmp_path / "submit-trace.zip",
        pre_submit_dom_path=tmp_path / "submit-dom-before.html",
        post_submit_dom_path=tmp_path / "submit-dom-after.html",
    )
    return GreenhouseHostedFormFlow(page, SubmissionPlan(source_kind="greenhouse", application_url=page.url), artifacts, SubmissionCapturePolicy())


@pytest.mark.anyio
async def test_greenhouse_confirmation_prefers_text() -> None:
    submitter = PlaywrightSubmitter()
    page = FakePage({"text=/thank you/i": FakeLocator(count=1, text="Thank you for applying")})
    text, strategy = await submitter._greenhouse_confirmation(page)
    assert text == "Thank you for applying"
    assert strategy == "explicit_success_text"


@pytest.mark.anyio
async def test_greenhouse_confirmation_falls_back_to_url() -> None:
    submitter = PlaywrightSubmitter()
    page = FakePage(url="https://boards.greenhouse.io/acme/thank_you")
    text, strategy = await submitter._greenhouse_confirmation(page)
    assert text == page.url
    assert strategy == "url_transition"


@pytest.mark.anyio
async def test_greenhouse_file_binding_uses_named_resume_input(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf", encoding="utf-8")
    page = FakePage({"input[type='file'][name=\"resume\"]": FakeLocator(count=1)})
    flow = build_flow(page, tmp_path)
    binding = FormFieldBinding(
        source_field_name="resume",
        widget_type="file",
        prompt_text="Resume/CV",
        required=True,
        artifact_binding=ArtifactBinding(artifact_kind=ArtifactKind.RESUME_PDF, source_artifact_kind=ArtifactKind.RESUME_PDF, path=str(resume), mime_type="application/pdf"),
    )
    bound, strategy = await flow._set_greenhouse_file(binding)
    assert bound is True
    assert strategy == "file_name"
    assert page.locator("input[type='file'][name=\"resume\"]").files == [str(resume)]



@pytest.mark.anyio
async def test_greenhouse_choice_binding_supports_react_comboboxes(tmp_path: Path) -> None:
    combobox_locator = FakeLocator(count=1, attributes={"role": "combobox", "aria-autocomplete": "list", "auto-select-on-blur": "true"})
    option_locator = FakeLocator(count=1, text="Yes")
    page = FakePage(
        selectors={
            "[name=\"authorized\"]": combobox_locator,
            "[role='option']": option_locator,
        }
    )
    flow = build_flow(page, tmp_path)
    binding = FormFieldBinding(
        source_field_name="authorized",
        widget_type="select",
        prompt_text="Authorized to work?",
        option_value="yes",
        value="Yes",
        metadata={
            "option_details": [{"label": "Yes", "value": "yes"}],
            "submission_binding": {"raw_type": "multi_value_single_select"},
        },
    )

    assert await flow._bind_greenhouse_choice(binding) == (True, "select_option")
    assert combobox_locator.filled == ["Yes"]
    assert option_locator.clicks == 1

@pytest.mark.anyio
async def test_greenhouse_choice_binding_supports_select_checkbox_and_radio(tmp_path: Path) -> None:
    select_locator = FakeLocator(count=1)
    checkbox_locator = FakeLocator(count=1)
    radio_locator = FakeLocator(count=1)
    page = FakePage(
        selectors={
            "[name=\"requires_sponsorship\"]": select_locator,
            "input[type='checkbox'][name=\"technologies\"][value=\"python\"]": checkbox_locator,
            "input[type='radio'][name=\"authorized\"][value=\"yes\"]": radio_locator,
        }
    )
    flow = build_flow(page, tmp_path)

    select_binding = FormFieldBinding(
        source_field_name="requires_sponsorship",
        widget_type="select",
        prompt_text="Will you require sponsorship?",
        value="No",
        option_value="no",
        metadata={"option_details": [{"label": "No", "value": "no"}]},
    )
    checkbox_binding = FormFieldBinding(
        source_field_name="technologies",
        widget_type="checkbox_group",
        prompt_text="Technologies",
        option_values=["python"],
        values=["Python"],
        metadata={"option_details": [{"label": "Python", "value": "python"}]},
    )
    radio_binding = FormFieldBinding(
        source_field_name="authorized",
        widget_type="radio_group",
        prompt_text="Authorized to work?",
        option_value="yes",
        value="Yes",
        metadata={"option_details": [{"label": "Yes", "value": "yes"}]},
    )

    assert await flow._bind_greenhouse_choice(select_binding) == (True, "select_option")
    assert await flow._bind_greenhouse_choice(checkbox_binding) == (True, "checkbox_group")
    assert await flow._bind_greenhouse_choice(radio_binding) == (True, "radio_group")
    assert select_locator.selected == [("value", "no")]
    assert checkbox_locator.checked is True
    assert radio_locator.checked is True


@pytest.mark.anyio
async def test_greenhouse_location_binding_clicks_autocomplete_option(monkeypatch, tmp_path: Path) -> None:
    location_locator = FakeLocator(count=1, attributes={"aria-autocomplete": "list", "auto-select-on-blur": "true"})
    option_locator = FakeLocator(count=1)
    page = FakePage(
        selectors={
            "[name=\"location\"]": location_locator,
            "[role='listbox'] [role='option']": option_locator,
        }
    )
    flow = build_flow(page, tmp_path)
    binding = FormFieldBinding(
        source_field_name="location",
        widget_type="text",
        prompt_text="Location",
        value="Chicago, IL",
        metadata={"section": "location"},
    )
    async def _fake_click_combobox_option(locator, candidate):
        _ = locator
        if candidate.startswith("Chicago"):
            option_locator.clicks += 1
            location_locator.attributes["has-selection"] = "true"
            return True
        return False

    monkeypatch.setattr(flow, "_click_combobox_option", _fake_click_combobox_option)
    bound, strategy = await flow._fill_greenhouse_text(binding)
    assert bound is True
    assert strategy == "name:location"
    assert location_locator.filled[0] == "Chicago"
    assert option_locator.clicks == 1


@pytest.mark.anyio
async def test_greenhouse_location_binding_raises_when_autocomplete_cannot_resolve(tmp_path: Path) -> None:
    page = FakePage({"[name=\"location\"]": FakeLocator(count=1, attributes={"aria-autocomplete": "list"})})
    flow = build_flow(page, tmp_path)
    binding = FormFieldBinding(
        source_field_name="location",
        widget_type="text",
        prompt_text="Location",
        value="Chicago, IL",
        metadata={"section": "location"},
    )
    with pytest.raises(GreenhouseBindingError) as exc_info:
        await flow._fill_greenhouse_text(binding)
    assert exc_info.value.reason == "location_autocomplete_failed"


def test_greenhouse_pre_submit_validation_classifies_binding_and_button_failures(tmp_path: Path) -> None:
    flow = build_flow(FakePage(), tmp_path)
    flow.field_audit = [{"required": True, "status": "error", "failure_reason": "file_upload_failed"}]
    assert flow._classify_pre_submit_failure({"submit_button_present": True, "submit_button_enabled": True, "visible_validation_errors": [], "missing_required_controls": []}) == (
        "file_upload_failed",
        "Required file upload failed before submit",
    )

    flow.field_audit = [{"required": True, "status": "bound", "failure_reason": None}]
    assert flow._classify_pre_submit_failure({"submit_button_present": True, "submit_button_enabled": False, "visible_validation_errors": [], "missing_required_controls": []}) == (
        "submit_button_disabled",
        "Greenhouse submit button is disabled",
    )
    assert flow._classify_pre_submit_failure({"submit_button_present": True, "submit_button_enabled": True, "visible_validation_errors": ["Please complete required fields"], "missing_required_controls": []}) == (
        "pre_submit_validation_failed",
        "Pre-submit validation failed",
    )


@pytest.mark.anyio
async def test_greenhouse_confirmation_detection_precedence_and_uncertain_classification(tmp_path: Path) -> None:
    flow = build_flow(
        FakePage(
            selectors={
                "text=/thank you/i": FakeLocator(count=1, text="Thank you for applying"),
                ".application_confirmation": FakeLocator(count=1, text="Application received"),
            },
            url="https://boards.greenhouse.io/acme/thank_you",
        ),
        tmp_path,
    )
    classification = await flow._classify_post_submit_result(
        "https://boards.greenhouse.io/acme/jobs/123",
        {"form_present": False, "visible_validation_errors": [], "missing_required_controls": []},
    )
    assert classification["submitted"] is True
    assert classification["confirmation_strategy"] == "explicit_success_text"

    uncertain = await build_flow(FakePage(url="https://boards.greenhouse.io/acme/jobs/123"), tmp_path)._classify_post_submit_result(
        "https://boards.greenhouse.io/acme/jobs/123",
        {"form_present": True, "visible_validation_errors": [], "missing_required_controls": []},
    )
    assert uncertain["submitted"] is False
    assert uncertain["failure_reason"] == "confirmation_not_detected"

    validation_error = await build_flow(FakePage(url="https://boards.greenhouse.io/acme/jobs/123"), tmp_path)._classify_post_submit_result(
        "https://boards.greenhouse.io/acme/jobs/123",
        {"form_present": True, "visible_validation_errors": ["Please correct the highlighted fields"], "missing_required_controls": []},
    )
    assert validation_error["submitted"] is False
    assert validation_error["failure_reason"] == "post_submit_validation_error"


def test_greenhouse_result_captures_expanded_evidence(tmp_path: Path) -> None:
    flow = build_flow(FakePage(url="https://boards.greenhouse.io/acme/thank_you"), tmp_path)
    flow.field_audit = [{"field": "resume", "status": "bound"}]
    for path in (
        flow.artifacts.pre_submit_snapshot_path,
        flow.artifacts.final_snapshot_path,
        flow.artifacts.pre_submit_dom_path,
        flow.artifacts.post_submit_dom_path,
    ):
        path.write_text("artifact", encoding="utf-8")
    result = flow._result(
        submitted=False,
        message="Submission outcome could not be confirmed",
        failure_reason="confirmation_not_detected",
        pre_state={"submit_button_present": True, "submit_button_enabled": True, "missing_required_controls": ["Resume/CV"], "visible_validation_errors": []},
        post_state={"visible_validation_errors": ["Unknown outcome"], "missing_required_controls": []},
        confirmation_text=None,
        confirmation_strategy=None,
        matched_confirmation_markers=["url_transition"],
    )
    evidence = result.evidence
    assert evidence is not None
    assert evidence.pre_submit_snapshot_path
    assert evidence.final_snapshot_path
    assert evidence.dom_snapshot_path
    assert evidence.post_submit_dom_snapshot_path
    assert evidence.failure_reason == "confirmation_not_detected"
    assert evidence.matched_confirmation_markers == ["url_transition"]
    assert evidence.missing_required_controls == ["Resume/CV"]
    assert evidence.visible_validation_errors == ["Unknown outcome"]

@pytest.mark.anyio
async def test_greenhouse_preview_stops_before_submit_and_returns_ready_for_review(tmp_path: Path) -> None:
    submit_locator = FakeLocator(count=1)
    page = FakePage(selectors={_GREENHOUSE_SUBMIT_SELECTOR: submit_locator})
    flow = build_flow(page, tmp_path)

    result = await flow.preview('https://boards.greenhouse.io/acme/jobs/123')

    assert result.status == JobLifecycleStatus.READY_FOR_REVIEW
    assert result.submitted is False
    assert result.uncertain is False
    assert submit_locator.clicks == 0
    assert flow.artifacts.pre_submit_snapshot_path.exists()
    assert flow.artifacts.final_snapshot_path.exists()
    assert flow.artifacts.pre_submit_dom_path.exists()
    assert flow.artifacts.post_submit_dom_path.exists()


@pytest.mark.anyio
async def test_preview_greenhouse_reports_playwright_runtime_blocked(monkeypatch, tmp_path: Path) -> None:
    submitter = PlaywrightSubmitter()

    async def _blocked_start() -> None:
        raise RuntimeError("Playwright runtime could not start: [WinError 5] Access is denied")

    monkeypatch.setattr(submitter, "_start_playwright_runtime", _blocked_start)

    result = await submitter.preview_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        SubmissionPlan(source_kind="greenhouse", application_url="https://boards.greenhouse.io/acme/jobs/123"),
        tmp_path,
    )

    assert isinstance(result, SubmissionResult)
    assert result.submitted is False
    assert result.uncertain is True
    assert "Playwright runtime could not start" in str(result.message)
    assert result.evidence is not None
    assert result.evidence.failure_reason == "playwright_runtime_blocked"


def test_lever_fixture_extraction_and_dom_analysis_detect_submit_and_captcha() -> None:
    html = Path('tmp_lever_apply_004.html').read_text(encoding='utf-8', errors='ignore')

    extraction = extract_questions_from_html(html, handoff_url='https://jobs.lever.co/plaid/apply')
    findings = analyze_dom_snapshot(html)

    assert extraction.raw_form['field_count'] > 10
    assert any(question.source_field_name == 'name' for question in extraction.questions)
    assert any(question.source_field_name == 'email' for question in extraction.questions)
    assert any(question.source_field_name == 'resume' for question in extraction.questions)
    assert findings['has_form'] is True
    assert findings['has_submit_button'] is True
    assert findings['has_captcha'] is True
    assert any(issue.startswith('captcha:') for issue in findings['detected_issues'])


@pytest.mark.anyio
async def test_preview_generic_form_handles_lever_fixture_without_clicking_submit(monkeypatch, tmp_path: Path) -> None:
    submitter = PlaywrightSubmitter()
    html = Path('tmp_lever_apply_004.html').read_text(encoding='utf-8', errors='ignore')
    page = FakePage(
        selectors={_GENERIC_SUBMIT_SELECTOR: FakeLocator(count=1)},
        url='https://jobs.lever.co/plaid/1e10caf7-85ed-4610-842f-78cea7d40de7/apply',
        content_html=html,
    )

    async def fake_start_runtime():
        return object()

    async def fake_open_browser_session(playwright, *, prefer_attached=False):
        _ = (playwright, prefer_attached)
        return {'context': FakeContext(), 'page': page}

    async def fake_close_browser_session(session):
        _ = session
        return None

    async def fake_stop_runtime(playwright):
        _ = playwright
        return None

    async def fake_attempt_captcha_solve(current_page, current_html, current_url):
        _ = (current_page, current_html, current_url)
        return True

    monkeypatch.setattr(submitter, '_start_playwright_runtime', fake_start_runtime)
    monkeypatch.setattr(submitter, '_open_browser_session', fake_open_browser_session)
    monkeypatch.setattr(submitter, '_close_browser_session', fake_close_browser_session)
    monkeypatch.setattr(submitter, '_stop_playwright_runtime', fake_stop_runtime)
    monkeypatch.setattr(submitter, '_attempt_captcha_solve', fake_attempt_captcha_solve)

    result = await submitter.preview_generic_form(
        'https://jobs.lever.co/plaid/1e10caf7-85ed-4610-842f-78cea7d40de7/apply',
        SubmissionPlan(source_kind='lever', application_url='https://jobs.lever.co/plaid/1e10caf7-85ed-4610-842f-78cea7d40de7/apply'),
        tmp_path,
    )

    assert result.status == JobLifecycleStatus.READY_FOR_REVIEW
    assert result.submitted is False
    assert result.uncertain is False
    assert result.evidence is not None
    assert result.evidence.submit_button_present is True
    assert result.evidence.confirmation_strategy == 'pre_submit_preview'


@pytest.mark.anyio
async def test_preview_generic_form_keep_browser_open_prefers_attached_session(monkeypatch, tmp_path: Path) -> None:
    submitter = PlaywrightSubmitter()
    page = FakePage(
        selectors={_GENERIC_SUBMIT_SELECTOR: FakeLocator(count=1)},
        url='https://jobs.lever.co/plaid/1e10caf7-85ed-4610-842f-78cea7d40de7/apply',
        content_html='<html><body><form><button type="submit">Submit</button></form></body></html>',
    )
    close_calls: list[dict] = []
    stop_calls: list[object] = []

    async def fake_start_runtime():
        return object()

    async def fake_open_browser_session(playwright, *, prefer_attached=False):
        _ = playwright
        assert prefer_attached is True
        return {'context': FakeContext(), 'page': page, 'attached': True}

    async def fake_close_browser_session(session):
        close_calls.append(session)
        return None

    async def fake_stop_runtime(playwright):
        stop_calls.append(playwright)
        return None

    async def fake_attempt_captcha_solve(current_page, current_html, current_url):
        _ = (current_page, current_html, current_url)
        return True

    monkeypatch.setattr(submitter, '_start_playwright_runtime', fake_start_runtime)
    monkeypatch.setattr(submitter, '_open_browser_session', fake_open_browser_session)
    monkeypatch.setattr(submitter, '_close_browser_session', fake_close_browser_session)
    monkeypatch.setattr(submitter, '_stop_playwright_runtime', fake_stop_runtime)
    monkeypatch.setattr(submitter, '_attempt_captcha_solve', fake_attempt_captcha_solve)

    result = await submitter.preview_generic_form(
        'https://jobs.lever.co/plaid/1e10caf7-85ed-4610-842f-78cea7d40de7/apply',
        SubmissionPlan(source_kind='lever', application_url='https://jobs.lever.co/plaid/1e10caf7-85ed-4610-842f-78cea7d40de7/apply'),
        tmp_path,
        keep_browser_open=True,
    )

    assert result.status == JobLifecycleStatus.READY_FOR_REVIEW
    assert result.evidence is not None
    assert result.evidence.browser_left_open is True
    assert close_calls == []
    assert len(stop_calls) == 1

