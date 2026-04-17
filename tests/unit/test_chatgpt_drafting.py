from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from pypdf import PdfWriter

from findmyjob.documents.pipeline import RenderedArtifact
from findmyjob.filefirst.chatgpt_drafting import (
    ChatGPTDraftingService,
    _RETRY_ATTACHMENTS_PROMPT,
    build_chatgpt_prompt,
    classify_downloads,
    extract_marked_block,
)
from findmyjob.filefirst.models import FileFact
from findmyjob.filefirst.workspace import FileWorkspace


def _reset_chatgpt_service_state() -> None:
    ChatGPTDraftingService._active_draft_workers.clear()
    ChatGPTDraftingService._claimed_download_entries.clear()
    ChatGPTDraftingService._deferred_browser_cleanups.clear()
    ChatGPTDraftingService._active_cdp_clients = 0
    ChatGPTDraftingService._next_prompt_submit_at = 0.0


def _workspace(tmp_path: Path) -> FileWorkspace:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv("# Test User\n")
    ws.save_facts([FileFact(fact_id="contact.primary", kind="contact", payload={"name": "Test User", "email": "user@example.com"})])
    return ws


def _blank_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)


def _mock_pdf_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        "findmyjob.documents.pipeline.DocumentPipeline.validate_pdf",
        lambda self, path, expect_one_page, context: {
            "valid": True,
            "page_count": 1,
            "expect_one_page": expect_one_page,
            "selected_fact_ids": context.get("selected_fact_ids", []),
        },
    )

    def _write_resume(self, base_name, pdf_artifact, context):
        _ = (pdf_artifact, context)
        path = self.artifacts_dir / f"{base_name}.resume.txt"
        path.write_text("Test User\nuser@example.com\n", encoding="utf-8")
        return RenderedArtifact(kind="resume", path=path, content_hash="resume-text", validation_results={"valid": True})

    def _write_cover(self, base_name, pdf_artifact, context):
        _ = (pdf_artifact, context)
        path = self.artifacts_dir / f"{base_name}.cover_letter.txt"
        path.write_text("Test User\nuser@example.com\nAcme\n", encoding="utf-8")
        return RenderedArtifact(kind="cover_letter", path=path, content_hash="cover-text", validation_results={"valid": True})

    monkeypatch.setattr("findmyjob.documents.pipeline.DocumentPipeline.write_resume_text_from_pdf", _write_resume)
    monkeypatch.setattr("findmyjob.documents.pipeline.DocumentPipeline.write_cover_letter_text_from_pdf", _write_cover)


def test_build_chatgpt_prompt_includes_local_date_company_role_and_stripped_description() -> None:
    _reset_chatgpt_service_state()
    prompt = build_chatgpt_prompt(company="Acme", role="Backend Engineer", job_description="<div>Build ML &amp; Python systems.</div>")

    assert "Current local date:" in prompt
    assert "Company: Acme" in prompt
    assert "Role: Backend Engineer" in prompt
    assert "Build ML & Python systems." in prompt
    assert "[[PDF_OUTPUT_READY]]" in prompt
    assert "Do not output sandbox:/mnt/data paths." in prompt
    assert "<div>" not in prompt


def test_extract_marked_block_requires_markers_in_order() -> None:
    _reset_chatgpt_service_state()
    text = "[[PDF_OUTPUT_READY]] one [[PDF_OUTPUT_COMPLETE]]"

    assert extract_marked_block(
        text,
        start_marker="[[PDF_OUTPUT_READY]]",
        end_marker="[[PDF_OUTPUT_COMPLETE]]",
    ) == "one"

    try:
        extract_marked_block("[[PDF_OUTPUT_COMPLETE]] late [[PDF_OUTPUT_READY]]", start_marker="[[PDF_OUTPUT_READY]]", end_marker="[[PDF_OUTPUT_COMPLETE]]")
        raise AssertionError("Expected out-of-order markers to fail")
    except ValueError as exc:
        assert str(exc) == "completion_markers_out_of_order"


def test_classify_downloads_requires_resume_and_cover_letter_filenames(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    resume = tmp_path / "Test_User_Acme_Backend_Resume.pdf"
    cover = tmp_path / "Test_User_Acme_Backend_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)

    classified = classify_downloads([resume, cover])

    assert classified.resume_raw_path == resume
    assert classified.cover_letter_raw_path == cover

    bad = tmp_path / "download.pdf"
    _blank_pdf(bad)
    try:
        classify_downloads([resume, bad])
        raise AssertionError("Expected filename mismatch to fail")
    except RuntimeError as exc:
        assert "unexpected_pdf_filename" in str(exc)


def test_normalize_artifacts_writes_canonical_files_and_extracted_text(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_dir = service.config.chatgpt_downloads_dir(ws.root) / "001" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    resume = raw_dir / "Test_User_Acme_Backend_Resume.pdf"
    cover = raw_dir / "Test_User_Acme_Backend_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)

    _mock_pdf_pipeline(monkeypatch)

    normalized = service._normalize_artifacts(
        raw_paths=[resume, cover],
        company="Acme",
        role="Backend Engineer",
        application_id="001",
        on_date="2026-04-12",
    )

    assert normalized["pdf_path"].exists()
    assert normalized["cover_letter_path"].exists()
    assert normalized["resume_text_path"].read_text(encoding="utf-8") == "Test User\nuser@example.com\n"
    assert normalized["cover_letter_text_path"].read_text(encoding="utf-8") == "Test User\nuser@example.com\nAcme\n"


def test_reuse_existing_downloads_copies_manual_files_into_raw(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_dir = service.config.chatgpt_downloads_dir(ws.root) / "006" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    resume = downloads_dir / "Jordan_Mercer_Notion_AI_Applications_Engineer_Resume.pdf"
    cover = downloads_dir / "Jordan_Mercer_Notion_AI_Applications_Engineer_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)

    monkeypatch.setattr(service, "_download_watch_dirs", lambda raw_root: [raw_root, downloads_dir])

    reused = service._reuse_existing_downloads(
        raw_root=raw_dir,
        company="Notion",
        role="AI Applications Engineer",
    )

    assert reused is not None
    assert all(path.parent == raw_dir for path in reused)
    assert {path.name for path in reused} == {
        "Test_User_Notion_AI_Applications_Engineer_Resume.pdf",
        "Test_User_Notion_AI_Applications_Engineer_Cover_Letter.pdf",
    }


def test_find_download_element_with_retry_reprompts_more_than_once(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    prompts: list[str] = []
    marker_attempts = {"count": 0}
    fake_download_locator = object()

    async def _fake_sleep(_: float) -> None:
        return None

    class _FakeTurnLocator:
        async def inner_text(self, timeout: int = 0) -> str:
            _ = timeout
            return (
                "[[PDF_OUTPUT_READY]]\n"
                "resume PDF link\n"
                "cover letter PDF link\n"
                "[[PDF_OUTPUT_COMPLETE]]"
            )

    async def _fake_find_marker_download_element(turn_locator, label):
        _ = (turn_locator, label)
        marker_attempts["count"] += 1
        if marker_attempts["count"] < 25:
            return None
        return fake_download_locator

    async def _fake_latest_marked_turn(page):
        _ = page
        return {"locator": _FakeTurnLocator(), "text": "done"}

    async def _fake_submit_prompt(page, prompt):
        _ = page
        prompts.append(prompt)

    async def _fake_wait_for_completed_turn(page):
        _ = page
        return {"locator": _FakeTurnLocator(), "text": "done"}

    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)
    monkeypatch.setattr(service, "_find_marker_download_element", _fake_find_marker_download_element)
    monkeypatch.setattr(service, "_latest_marked_turn", _fake_latest_marked_turn)
    monkeypatch.setattr(service, "_submit_prompt", _fake_submit_prompt)
    monkeypatch.setattr(service, "_wait_for_completed_turn", _fake_wait_for_completed_turn)

    turn_locator, locator = asyncio.run(
        service._find_download_element_with_retry(page=object(), turn_locator=_FakeTurnLocator(), label="resume")
    )

    assert locator is fake_download_locator
    assert isinstance(turn_locator, _FakeTurnLocator)
    assert prompts == [_RETRY_ATTACHMENTS_PROMPT, _RETRY_ATTACHMENTS_PROMPT]


def test_cdp_cleanup_waits_for_last_active_worker() -> None:
    _reset_chatgpt_service_state()
    ChatGPTDraftingService._register_cdp_client()
    ChatGPTDraftingService._register_cdp_client()

    first = ChatGPTDraftingService._release_cdp_client(browser="browser-1", playwright="pw-1")
    second = ChatGPTDraftingService._release_cdp_client(browser="browser-2", playwright="pw-2")

    assert first == []
    assert second == [("browser-1", "pw-1"), ("browser-2", "pw-2")]


def test_enable_temporary_chat_clicks_toggle_when_available(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    attempts: list[tuple[str, int]] = []

    class _FakeToggle:
        def __init__(self) -> None:
            self.clicked = False

        async def click(self) -> None:
            self.clicked = True
            page.url = "https://chatgpt.com/?temporary-chat=true"

    toggle = _FakeToggle()
    page = type("FakePage", (), {"url": "https://chatgpt.com/"})()

    async def _fake_try_find(page, selectors, *, timeout_ms):
        _ = page
        attempts.append((selectors[0], timeout_ms))
        if selectors == ("button[aria-label='Turn off temporary chat']", "button[aria-label*='temporary chat'][aria-pressed='true']"):
            return None
        return toggle

    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(service, "_try_find_visible_locator", _fake_try_find)
    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)

    enabled = asyncio.run(service._enable_temporary_chat(page=page))

    assert enabled is True
    assert toggle.clicked is True
    assert len(attempts) >= 2


def test_launch_browser_uses_explicit_start_url_override(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    captured: dict[str, object] = {}

    monkeypatch.setattr("findmyjob.apply.browser_session._is_cdp_listening", lambda port: False)

    def _fake_launch_attachable_browser(*, browser_cdp_url, profile_dir, start_url):
        captured["browser_cdp_url"] = browser_cdp_url
        captured["profile_dir"] = profile_dir
        captured["start_url"] = start_url
        return True

    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.launch_attachable_browser", _fake_launch_attachable_browser)
    monkeypatch.setattr(service, "_probe_existing_browser_health", lambda: {"healthy": True, "reason": None})

    payload = service.launch_browser(start_url="about:blank")

    assert payload["launched"] is True
    assert captured["start_url"] == "about:blank"


def test_launch_browser_relaunches_unhealthy_existing_browser(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    health_checks = iter(
        [
            {"healthy": False, "reason": "chatgpt_http_431:stale_session"},
            {"healthy": True, "reason": None},
        ]
    )
    launches: list[dict[str, object]] = []
    taskkills: list[list[str]] = []

    monkeypatch.setattr("findmyjob.apply.browser_session._is_cdp_listening", lambda port: True)
    monkeypatch.setattr(service, "_probe_existing_browser_health", lambda: next(health_checks))
    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.time.sleep", lambda _: None)

    def _fake_launch_attachable_browser(*, browser_cdp_url, profile_dir, start_url):
        launches.append(
            {
                "browser_cdp_url": browser_cdp_url,
                "profile_dir": profile_dir,
                "start_url": start_url,
            }
        )
        return True

    def _fake_run(command, capture_output=True, timeout=0):
        _ = (capture_output, timeout)
        taskkills.append(list(command))
        return None

    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.launch_attachable_browser", _fake_launch_attachable_browser)
    monkeypatch.setattr("subprocess.run", _fake_run)

    payload = service.launch_browser(start_url="about:blank")

    assert payload["launched"] is True
    assert launches and launches[0]["start_url"] == "about:blank"
    assert taskkills and taskkills[0][:3] == ["taskkill", "/IM", "chrome.exe"]
    assert "relaunching the dedicated browser" in str(payload.get("note") or "").casefold()


def test_draft_sync_relaunches_browser_once_on_chatgpt_http_431(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_dir = service.config.chatgpt_downloads_dir(ws.root) / "001" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    resume = raw_dir / "Test_User_Acme_Backend_Engineer_Resume.pdf"
    cover = raw_dir / "Test_User_Acme_Backend_Engineer_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)

    launch_calls: list[tuple[bool, str | None]] = []
    draft_attempts = {"count": 0}

    monkeypatch.setattr(service, "_reuse_existing_downloads", lambda **kwargs: None)
    monkeypatch.setattr(
        service,
        "_normalize_artifacts",
        lambda **kwargs: {
            "pdf_path": ws.root / "output" / "cv-001-acme.pdf",
            "cover_letter_path": ws.root / "output" / "cover-letter-001-acme.pdf",
            "resume_text_path": ws.root / "output" / "cv-001-acme.txt",
            "cover_letter_text_path": ws.root / "output" / "cover-letter-001-acme.txt",
        },
    )
    monkeypatch.setattr(
        service,
        "launch_browser",
        lambda *, close_existing=False, start_url=None: launch_calls.append((close_existing, start_url)) or {"launched": True},
    )

    def _fake_run_async(func, *args):
        _ = (func, args)
        draft_attempts["count"] += 1
        if draft_attempts["count"] == 1:
            raise RuntimeError("chatgpt_http_431:stale_session")
        return {"downloads": [resume, cover], "assistant_text": ""}

    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.run_async", _fake_run_async)

    job = type("FakeJob", (), {"job_id": "job-1", "company": "Acme", "title": "Backend Engineer", "description": "Build systems.", "date": "2026-04-15"})()
    evaluation = type("FakeEvaluation", (), {"company": "Acme", "role": "Backend Engineer"})()

    result = service._draft_sync(job=job, evaluation=evaluation, application_id="001", on_date="2026-04-15")

    assert result["success"] is True
    assert draft_attempts["count"] == 2
    assert launch_calls == [(False, "about:blank"), (True, "about:blank")]


def test_reserve_prompt_submission_slot_spaces_concurrent_submissions(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    _workspace(tmp_path)
    timeline = iter([100.0, 100.0, 101.5])
    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.time.monotonic", lambda: next(timeline))

    first_wait = ChatGPTDraftingService._reserve_prompt_submission_slot(6.0)
    second_wait = ChatGPTDraftingService._reserve_prompt_submission_slot(6.0)
    third_wait = ChatGPTDraftingService._reserve_prompt_submission_slot(6.0)

    assert first_wait == 0.0
    assert second_wait == 6.0
    assert third_wait == 10.5


def test_with_temporary_chat_query_adds_or_preserves_query(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    assert service._with_temporary_chat_query("https://chatgpt.com/g/demo").endswith("?temporary-chat=true")
    assert service._with_temporary_chat_query("https://chatgpt.com/g/demo?foo=bar").endswith("foo=bar&temporary-chat=true")


def test_open_cdp_drafting_page_serializes_connection_and_sets_page_timeout(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    class _FakePage:
        def __init__(self) -> None:
            self.timeout_ms = None

        def set_default_timeout(self, value: int) -> None:
            self.timeout_ms = value

    fake_page = _FakePage()
    fake_context = type("FakeContext", (), {"new_page": AsyncMock(return_value=fake_page)})()
    fake_browser = type("FakeBrowser", (), {"contexts": [fake_context]})()
    fake_chromium = type("FakeChromium", (), {"connect_over_cdp": AsyncMock(return_value=fake_browser)})()
    fake_playwright = type("FakePlaywright", (), {"chromium": fake_chromium})()

    browser, page = asyncio.run(service._open_cdp_drafting_page(fake_playwright))

    assert browser is fake_browser
    assert page is fake_page
    assert fake_page.timeout_ms == int(service.drafting.timeout_seconds * 1000)
    fake_chromium.connect_over_cdp.assert_awaited_once_with(service.drafting.browser_cdp_url)
    fake_context.new_page.assert_awaited_once()


def test_submit_prompt_waits_for_enabled_send_button_before_clicking(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    class _FakeSendButton:
        def __init__(self) -> None:
            self.clicks = 0
            self.enabled_checks = 0

        async def is_visible(self, timeout: int = 0) -> bool:
            _ = timeout
            return True

        async def is_enabled(self, timeout: int = 0) -> bool:
            _ = timeout
            self.enabled_checks += 1
            return self.enabled_checks >= 3

        async def click(self) -> None:
            self.clicks += 1

    class _FakeLocatorGroup:
        def __init__(self, button: _FakeSendButton) -> None:
            self.first = button

    class _FakeKeyboard:
        def __init__(self) -> None:
            self.presses: list[str] = []

        async def press(self, key: str) -> None:
            self.presses.append(key)

        async def insert_text(self, text: str) -> None:
            _ = text

    class _FakePage:
        def __init__(self, button: _FakeSendButton) -> None:
            self.keyboard = _FakeKeyboard()
            self._button = button

        async def bring_to_front(self) -> None:
            return None

        def locator(self, selector: str):
            assert selector in (
                "button[data-testid='send-button']",
                "button[aria-label*='Send']",
                "button[aria-label*='send']",
                "button[class*='send']",
            )
            return _FakeLocatorGroup(self._button)

    fake_button = _FakeSendButton()
    page = _FakePage(fake_button)
    class _FakeComposer:
        async def click(self) -> None:
            return None

        async def evaluate(self, script: str):
            _ = script
            return None

    composer = _FakeComposer()

    async def _fake_wait_for_prompt_composer(page_arg, timeout_ms: int = 0):
        _ = (page_arg, timeout_ms)
        return composer

    async def _fake_write_prompt(page_arg, composer_arg, prompt: str) -> None:
        _ = (page_arg, composer_arg, prompt)
        return None

    async def _fake_has_prompt_fragment(composer_arg, prompt: str) -> bool:
        _ = (composer_arg, prompt)
        return True

    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(service, "_wait_for_prompt_composer", _fake_wait_for_prompt_composer)
    monkeypatch.setattr(service, "_write_prompt_to_composer", _fake_write_prompt)
    monkeypatch.setattr(service, "_composer_has_prompt_fragment", _fake_has_prompt_fragment)
    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)

    asyncio.run(service._submit_prompt(page, "hello world"))

    assert fake_button.clicks == 1
    assert "Enter" not in page.keyboard.presses


def test_wait_for_new_pdf_ignores_wrong_job_files_in_shared_download_dir(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    shared_dir = tmp_path / "downloads"
    raw_root = ws.root / ".fmj" / "runtime" / "chatgpt-downloads" / "001" / "raw"
    shared_dir.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    wrong_resume = shared_dir / "Jordan_Mercer_Cloudflare_Systems_Engineer_MAPS_Resume.pdf"
    correct_resume = shared_dir / "Jordan_Mercer_Acme_Backend_Engineer_Resume.pdf"
    _blank_pdf(wrong_resume)
    _blank_pdf(correct_resume)

    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)

    selected = asyncio.run(
        service._wait_for_new_pdf(
            [shared_dir],
            {},
            raw_root=raw_root,
            company="Acme",
            role="Backend Engineer",
            label="resume",
            claim_key="app-001",
            timeout=2.0,
        )
    )

    assert selected == correct_resume


def test_wait_for_new_pdf_falls_back_to_single_new_pdf_in_serial_mode(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    service.drafting.max_parallel_jobs = 1
    shared_dir = tmp_path / "downloads"
    raw_root = ws.root / ".fmj" / "runtime" / "chatgpt-downloads" / "001" / "raw"
    shared_dir.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    generic_pdf = shared_dir / "download.pdf"
    _blank_pdf(generic_pdf)

    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)

    selected = asyncio.run(
        service._wait_for_new_pdf(
            [shared_dir],
            {},
            raw_root=raw_root,
            company="Acme",
            role="Backend Engineer",
            label="resume",
            claim_key="app-001",
            timeout=2.0,
        )
    )

    assert selected == generic_pdf


def test_translate_chatgpt_navigation_error_promotes_http_431(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    class _FakeBody:
        async def inner_text(self, timeout: int = 0) -> str:
            _ = timeout
            return "This page isn’t working HTTP ERROR 431 Reload"

    class _FakePage:
        url = "chrome-error://chromewebdata/"

        async def title(self) -> str:
            return "chatgpt.com"

        def locator(self, selector: str):
            assert selector == "body"
            return _FakeBody()

    error = asyncio.run(
        service._translate_chatgpt_navigation_error(
            _FakePage(),
            "https://chatgpt.com/g/custom",
            RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"),
        )
    )

    assert isinstance(error, RuntimeError)
    assert str(error).startswith("chatgpt_http_431:")


def test_wait_for_prompt_composer_reloads_once_before_failing(tmp_path: Path, monkeypatch) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    fake_composer = object()
    attempts = {"count": 0}

    class _FakeBodyLocator:
        async def inner_text(self, timeout: int = 0) -> str:
            _ = timeout
            return ""

    class _FakePage:
        def __init__(self) -> None:
            self.url = "https://chatgpt.com/g/demo?temporary-chat=true"
            self.goto_calls: list[str] = []
            self.bring_to_front_calls = 0

        async def bring_to_front(self) -> None:
            self.bring_to_front_calls += 1

        def locator(self, selector: str):
            assert selector == "body"
            return _FakeBodyLocator()

        async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
            _ = wait_until
            self.goto_calls.append(url)
            self.url = url

        async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
            _ = (state, timeout)
            return None

        async def title(self) -> str:
            return "ChatGPT"

    page = _FakePage()

    async def _fake_try_find(page_obj, selectors, *, timeout_ms):
        _ = (page_obj, selectors, timeout_ms)
        attempts["count"] += 1
        if attempts["count"] < 2:
            return None
        return fake_composer

    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(service, "_try_find_visible_locator", _fake_try_find)
    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)

    composer = asyncio.run(
        service._wait_for_prompt_composer(
            page,
            timeout_ms=60_000,
            recovery_url="https://chatgpt.com/g/demo?temporary-chat=true",
        )
    )

    assert composer is fake_composer
    assert page.goto_calls == ["https://chatgpt.com/g/demo?temporary-chat=true"]
    assert page.bring_to_front_calls >= 1


def test_update_status_preserves_existing_batch_when_first_read_is_stale(tmp_path: Path, monkeypatch) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    coordinator = ChatGPTDraftingService(ws)
    coordinator.start_batch(
        run_id="auto-1",
        run_type="autonomous",
        target_size=2,
        members=[
            {"application_id": "001", "job_id": "job-001", "company": "Acme", "role": "Backend Engineer"},
            {"application_id": "002", "job_id": "job-002", "company": "Acme", "role": "Frontend Engineer"},
        ],
    )
    worker = ChatGPTDraftingService(ws)
    worker._register_draft_worker(
        application_id="001",
        job_id="job-001",
        company="Acme",
        role="Backend Engineer",
    )
    original_load = type(ws).load_chatgpt_drafting_status
    calls = {"count": 0}

    def _flaky_load(self):
        calls["count"] += 1
        if calls["count"] == 1:
            return {}
        return original_load(self)

    monkeypatch.setattr(type(ws), "load_chatgpt_drafting_status", _flaky_load)

    worker._update_status(status="running", phase="loading_gpt", last_observation="Opening ChatGPT home to enable temporary chat.")

    saved = original_load(ws)
    assert saved["batch"]["run_type"] == "autonomous"
    assert saved["batch"]["member_count"] == 2
    assert saved["batch"]["members"][0]["application_id"] == "001"
    assert saved["batch"]["members"][1]["application_id"] == "002"


def test_marked_turn_has_sandbox_paths(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    assert service._marked_turn_has_sandbox_paths("sandbox:/mnt/data/file.pdf") is True
    assert service._marked_turn_has_sandbox_paths("/mnt/data/file.pdf") is True
    assert service._marked_turn_has_sandbox_paths("Failed to get upload status for /mnt/data/file.pdf") is True
    assert service._marked_turn_has_sandbox_paths("[[PDF_OUTPUT_READY]]\nResume PDF\n[[PDF_OUTPUT_COMPLETE]]") is False


def test_should_retry_without_temporary_chat_for_upload_status_errors(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    assert service._should_retry_without_temporary_chat(RuntimeError("/mnt/data/resume.pdf")) is True
    assert service._should_retry_without_temporary_chat(
        RuntimeError("Failed to get upload status for /mnt/data/Jordan_Mercer_Cloudflare_Systems_Engineer_MAPS_Resume.pdf")
    ) is True


def test_download_pdfs_brings_tab_to_front_before_clicking(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_root = ws.root / ".fmj" / "runtime" / "download-front" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    resume = raw_root / "Test_User_Acme_Backend_Resume.pdf"
    cover = raw_root / "Test_User_Acme_Backend_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)
    waits = iter([resume, cover])

    class _FakeLocator:
        def __init__(self) -> None:
            self.clicks = 0

        async def scroll_into_view_if_needed(self) -> None:
            return None

        async def click(self) -> None:
            self.clicks += 1

    class _FakeCdpSession:
        async def send(self, method: str, payload: dict[str, str]) -> None:
            _ = (method, payload)
            return None

    class _FakeContext:
        async def new_cdp_session(self, page) -> _FakeCdpSession:
            _ = page
            return _FakeCdpSession()

    class _FakeTurnLocator:
        async def inner_text(self, timeout: int = 0) -> str:
            _ = timeout
            return "[[PDF_OUTPUT_READY]]\nResume PDF\nCover Letter PDF\n[[PDF_OUTPUT_COMPLETE]]"

    class _FakePage:
        def __init__(self) -> None:
            self.context = _FakeContext()
            self.brought_to_front = 0

        async def bring_to_front(self) -> None:
            self.brought_to_front += 1

    async def _fake_find(page, turn_locator, label):
        _ = (page, turn_locator, label)
        return turn_locator, _FakeLocator()

    async def _fake_wait(*args, **kwargs):
        _ = (args, kwargs)
        return next(waits)

    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(service, "_find_download_element_with_retry", _fake_find)
    monkeypatch.setattr(service, "_wait_for_new_pdf", _fake_wait)
    monkeypatch.setattr(service, "_download_watch_dirs", lambda raw: [raw])
    monkeypatch.setattr(service, "_snapshot_download_entries", lambda directories: {})
    monkeypatch.setattr("findmyjob.filefirst.chatgpt_drafting.anyio.sleep", _fake_sleep)

    page = _FakePage()
    downloads = asyncio.run(
        service._download_pdfs(page, _FakeTurnLocator(), raw_root, company="Acme", role="Backend Engineer")
    )

    assert page.brought_to_front >= 2
    assert downloads == [
        raw_root / "Test_User_Acme_Backend_Engineer_Resume.pdf",
        raw_root / "Test_User_Acme_Backend_Engineer_Cover_Letter.pdf",
    ]


def test_draft_sync_uses_configured_non_temporary_chat_by_default(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    calls: list[bool] = []
    raw_root = ws.root / ".fmj" / "runtime" / "chatgpt-downloads" / "001" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    resume = raw_root / "Test_User_Acme_Backend_Resume.pdf"
    cover = raw_root / "Test_User_Acme_Backend_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)

    class _Evaluation:
        company = "Acme"
        role = "Backend Engineer"

    class _Job:
        job_id = "job-001"
        company = "Acme"
        title = "Backend Engineer"
        description = "Build Python systems."

    async def _fake_draft_via_browser(prompt, raw_root_path, company, role, use_temporary_chat=False):
        _ = (prompt, raw_root_path, company, role)
        calls.append(bool(use_temporary_chat))
        return {"downloads": [resume, cover], "assistant_text": ""}

    monkeypatch.setattr(service, "_reuse_existing_downloads", lambda **kwargs: None)
    monkeypatch.setattr(service, "_draft_via_browser", _fake_draft_via_browser)
    monkeypatch.setattr(service, "launch_browser", lambda *, close_existing=False, start_url=None: {"launched": True, "close_existing": close_existing, "start_url": start_url})
    monkeypatch.setattr(
        service,
        "_normalize_artifacts",
        lambda **kwargs: {
            "pdf_path": ws.output_dir / "cv-001-acme-2026-04-14.pdf",
            "cover_letter_path": ws.output_dir / "cover-letter-001-acme.pdf",
            "resume_text_path": ws.output_dir / "cv-001-acme-2026-04-14.resume.txt",
            "cover_letter_text_path": ws.output_dir / "cv-001-acme-2026-04-14.cover_letter.txt",
        },
    )

    result = service._draft_sync(job=_Job(), evaluation=_Evaluation(), application_id="001", on_date="2026-04-14")

    assert result["success"] is True
    assert calls == [False]
    assert service.workspace.load_chatgpt_drafting_status().get("temporary_chat_last_result") == "disabled_by_config"


def test_draft_sync_retries_once_without_temporary_chat_when_explicitly_enabled(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    service.drafting.use_temporary_chat = True
    calls: list[bool] = []
    raw_root = ws.root / ".fmj" / "runtime" / "chatgpt-downloads" / "001" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    resume = raw_root / "Test_User_Acme_Backend_Resume.pdf"
    cover = raw_root / "Test_User_Acme_Backend_Cover_Letter.pdf"
    _blank_pdf(resume)
    _blank_pdf(cover)

    class _Evaluation:
        company = "Acme"
        role = "Backend Engineer"

    class _Job:
        job_id = "job-001"
        company = "Acme"
        title = "Backend Engineer"
        description = "Build Python systems."

    async def _fake_draft_via_browser(prompt, raw_root_path, company, role, use_temporary_chat=False):
        _ = (prompt, raw_root_path, company, role)
        calls.append(bool(use_temporary_chat))
        if use_temporary_chat:
            raise RuntimeError("download_timed_out:resume")
        return {"downloads": [resume, cover], "assistant_text": ""}

    monkeypatch.setattr(service, "_reuse_existing_downloads", lambda **kwargs: None)
    monkeypatch.setattr(service, "_draft_via_browser", _fake_draft_via_browser)
    monkeypatch.setattr(service, "launch_browser", lambda *, close_existing=False, start_url=None: {"launched": True, "close_existing": close_existing, "start_url": start_url})
    monkeypatch.setattr(
        service,
        "_normalize_artifacts",
        lambda **kwargs: {
            "pdf_path": ws.output_dir / "cv-001-acme-2026-04-14.pdf",
            "cover_letter_path": ws.output_dir / "cover-letter-001-acme.pdf",
            "resume_text_path": ws.output_dir / "cv-001-acme-2026-04-14.resume.txt",
            "cover_letter_text_path": ws.output_dir / "cv-001-acme-2026-04-14.cover_letter.txt",
        },
    )

    result = service._draft_sync(job=_Job(), evaluation=_Evaluation(), application_id="001", on_date="2026-04-14")

    assert result["success"] is True
    assert calls == [True, False]


def test_recover_stale_batch_salvages_existing_downloads(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    _mock_pdf_pipeline(monkeypatch)

    raw_dir = service.config.chatgpt_downloads_dir(ws.root) / "001" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _blank_pdf(raw_dir / "Test_User_Acme_Backend_Resume.pdf")
    _blank_pdf(raw_dir / "Test_User_Acme_Backend_Cover_Letter.pdf")

    service.start_batch(
        run_id="auto-001",
        run_type="autonomous",
        target_size=1,
        members=[{"application_id": "001", "job_id": "job-001", "company": "Acme", "role": "Backend Engineer"}],
    )
    state = ws.load_chatgpt_drafting_status()
    state["status"] = "running"
    state["phase"] = "downloading_pdfs"
    state["batch"]["members"][0]["status"] = "running"
    state["batch"]["members"][0]["phase"] = "downloading_pdfs"
    ws.save_chatgpt_drafting_status(state)

    batch = service.recover_stale_batch()
    recovered_state = ws.load_chatgpt_drafting_status()

    assert batch is not None
    assert batch["completed_count"] == 1
    assert batch["failed_count"] == 0
    assert recovered_state["status"] == "completed"
    assert recovered_state["batch"]["members"][0]["status"] == "reused"
    assert Path(ws.root / recovered_state["last_result"]["pdf_path"]).exists()


def test_recover_stale_batch_marks_missing_downloads_failed(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)

    service.start_batch(
        run_id="auto-001",
        run_type="autonomous",
        target_size=1,
        members=[{"application_id": "001", "job_id": "job-001", "company": "Acme", "role": "Backend Engineer"}],
    )
    state = ws.load_chatgpt_drafting_status()
    state["status"] = "running"
    state["phase"] = "downloading_pdfs"
    state["batch"]["members"][0]["status"] = "running"
    state["batch"]["members"][0]["phase"] = "downloading_pdfs"
    ws.save_chatgpt_drafting_status(state)

    batch = service.recover_stale_batch()
    recovered_state = ws.load_chatgpt_drafting_status()

    assert batch is not None
    assert batch["completed_count"] == 0
    assert batch["failed_count"] == 1
    assert recovered_state["status"] == "failed"
    assert recovered_state["batch"]["members"][0]["status"] == "failed"


def test_batch_status_stays_running_until_last_worker_finishes(tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    first = ChatGPTDraftingService(ws)
    second = ChatGPTDraftingService(ws)

    first.start_batch(
        run_id="auto-001",
        run_type="autonomous",
        target_size=2,
        members=[
            {"application_id": "006", "job_id": "job-006", "company": "Notion", "role": "AI Applications Engineer"},
            {"application_id": "007", "job_id": "job-007", "company": "Notion", "role": "AI Conversation Designer"},
        ],
    )

    first._register_draft_worker(application_id="006", job_id="job-006", company="Notion", role="AI Applications Engineer")
    second._register_draft_worker(application_id="007", job_id="job-007", company="Notion", role="AI Conversation Designer")
    first._update_status(status="running", phase="waiting_for_markers", last_observation="first active")
    second._update_status(status="running", phase="waiting_for_markers", last_observation="second active")

    first._complete_draft_worker(
        result={
            "application_id": "006",
            "job_id": "job-006",
            "success": True,
            "renderer": "chatgpt_download",
            "pdf_path": "output/cv-006.pdf",
            "cover_letter_path": "output/cover-006.pdf",
            "render_error": None,
        }
    )

    mid_state = ws.load_chatgpt_drafting_status()
    assert mid_state["status"] == "running"
    assert mid_state["phase"] == "batch_running"
    assert mid_state["active_worker_count"] == 1
    assert mid_state["batch"]["member_count"] == 2
    assert mid_state["batch"]["completed_count"] == 1
    assert mid_state["batch"]["remaining_count"] == 1
    assert mid_state["batch"]["handoff_status"] == "waiting_for_batch"

    second._complete_draft_worker(
        result={
            "application_id": "007",
            "job_id": "job-007",
            "success": True,
            "renderer": "chatgpt_download",
            "pdf_path": "output/cv-007.pdf",
            "cover_letter_path": "output/cover-007.pdf",
            "render_error": None,
        }
    )

    final_state = ws.load_chatgpt_drafting_status()
    assert final_state["status"] == "completed"
    assert final_state["phase"] == "completed"
    assert "active_worker_count" not in final_state
    assert final_state["batch"]["completed_count"] == 2
    assert final_state["batch"]["remaining_count"] == 0
    assert final_state["batch"]["handoff_status"] == "ready_for_prepare"


def test_download_pdfs_reprompts_when_turn_uses_sandbox_paths(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_root = ws.root / ".fmj" / "runtime" / "download-test" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    initial_turn = {"locator": object(), "text": "[[PDF_OUTPUT_READY]]\nsandbox:/mnt/data/resume.pdf\nsandbox:/mnt/data/cover.pdf\n[[PDF_OUTPUT_COMPLETE]]"}
    recovered_turn = {"locator": object(), "text": "[[PDF_OUTPUT_READY]]\nResume PDF\nCover Letter PDF\n[[PDF_OUTPUT_COMPLETE]]"}

    class _FakeLocator:
        async def scroll_into_view_if_needed(self) -> None:
            return None

        async def click(self) -> None:
            return None

    state = {"count": 0}

    async def _fake_request_downloadable_attachments(page, *, reason):
        _ = (page, reason)
        return dict(recovered_turn)

    async def _fake_find_download_element_with_retry(page, turn_locator, label):
        _ = (page, turn_locator, label)
        return recovered_turn["locator"], _FakeLocator()

    async def _fake_wait_for_new_pdf(directories, known, *, raw_root, company, role, label, claim_key, timeout):
        _ = (directories, known, company, role, label, claim_key, timeout)
        state["count"] += 1
        path = raw_root / ("Test_User_Acme_Backend_Resume.pdf" if state["count"] == 1 else "Test_User_Acme_Backend_Cover_Letter.pdf")
        _blank_pdf(path)
        return path

    monkeypatch.setattr(service, "_request_downloadable_attachments", _fake_request_downloadable_attachments)
    monkeypatch.setattr(service, "_find_download_element_with_retry", _fake_find_download_element_with_retry)
    monkeypatch.setattr(service, "_wait_for_new_pdf", _fake_wait_for_new_pdf)
    monkeypatch.setattr(service, "_download_watch_dirs", lambda raw_root: [raw_root])
    monkeypatch.setattr(service, "_snapshot_download_entries", lambda directories: {})

    class _FakeCdpSession:
        async def send(self, method, params):
            _ = (method, params)
            return None

    class _FakeContext:
        async def new_cdp_session(self, page):
            _ = page
            return _FakeCdpSession()

    class _FakeTurnLocator:
        async def inner_text(self, timeout=0):
            _ = timeout
            return initial_turn["text"]

    class _FakePage:
        context = _FakeContext()

    downloads = asyncio.run(
        service._download_pdfs(_FakePage(), _FakeTurnLocator(), raw_root, company="Acme", role="Backend Engineer")
    )

    assert len(downloads) == 2
    assert downloads[0].name.endswith("_Resume.pdf")
    assert downloads[1].name.endswith("_Cover_Letter.pdf")


def test_download_pdfs_reprompts_when_turn_uses_upload_status_paths(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_root = ws.root / ".fmj" / "runtime" / "download-test-upload-status" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    recovered_turn = {"locator": object(), "text": "[[PDF_OUTPUT_READY]]\nResume PDF\nCover Letter PDF\n[[PDF_OUTPUT_COMPLETE]]"}
    reasons: list[str] = []

    class _FakeLocator:
        async def scroll_into_view_if_needed(self) -> None:
            return None

        async def click(self) -> None:
            return None

    state = {"count": 0}

    async def _fake_request_downloadable_attachments(page, *, reason):
        _ = page
        reasons.append(reason)
        return dict(recovered_turn)

    async def _fake_find_download_element_with_retry(page, turn_locator, label):
        _ = (page, turn_locator, label)
        return recovered_turn["locator"], _FakeLocator()

    async def _fake_wait_for_new_pdf(directories, known, *, raw_root, company, role, label, claim_key, timeout):
        _ = (directories, known, company, role, label, claim_key, timeout)
        state["count"] += 1
        path = raw_root / ("Test_User_Acme_Backend_Resume.pdf" if state["count"] == 1 else "Test_User_Acme_Backend_Cover_Letter.pdf")
        _blank_pdf(path)
        return path

    monkeypatch.setattr(service, "_request_downloadable_attachments", _fake_request_downloadable_attachments)
    monkeypatch.setattr(service, "_find_download_element_with_retry", _fake_find_download_element_with_retry)
    monkeypatch.setattr(service, "_wait_for_new_pdf", _fake_wait_for_new_pdf)
    monkeypatch.setattr(service, "_download_watch_dirs", lambda raw_root: [raw_root])
    monkeypatch.setattr(service, "_snapshot_download_entries", lambda directories: {})

    class _FakeCdpSession:
        async def send(self, method, params):
            _ = (method, params)
            return None

    class _FakeContext:
        async def new_cdp_session(self, page):
            _ = page
            return _FakeCdpSession()

    class _FakeTurnLocator:
        async def inner_text(self, timeout=0):
            _ = timeout
            return (
                "[[PDF_OUTPUT_READY]]\n"
                "Failed to get upload status for /mnt/data/Jordan_Mercer_Cloudflare_Systems_Engineer_MAPS_Resume.pdf\n"
                "/mnt/data/Jordan_Mercer_Cloudflare_Systems_Engineer_MAPS_Cover_Letter.pdf\n"
                "[[PDF_OUTPUT_COMPLETE]]"
            )

    class _FakePage:
        context = _FakeContext()

    downloads = asyncio.run(
        service._download_pdfs(_FakePage(), _FakeTurnLocator(), raw_root, company="Acme", role="Backend Engineer")
    )

    assert reasons == ["sandbox_paths"]
    assert len(downloads) == 2
    assert downloads[0].name.endswith("_Resume.pdf")
    assert downloads[1].name.endswith("_Cover_Letter.pdf")


def test_download_pdfs_in_temporary_chat_fails_over_after_first_timeout(monkeypatch, tmp_path: Path) -> None:
    _reset_chatgpt_service_state()
    ws = _workspace(tmp_path)
    service = ChatGPTDraftingService(ws)
    raw_root = ws.root / ".fmj" / "runtime" / "download-temp-fallback" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    class _FakeLocator:
        async def scroll_into_view_if_needed(self) -> None:
            return None

        async def click(self) -> None:
            return None

    class _FakeCdpSession:
        async def send(self, method, params):
            _ = (method, params)
            return None

    class _FakeContext:
        async def new_cdp_session(self, page):
            _ = page
            return _FakeCdpSession()

    class _FakeTurnLocator:
        async def inner_text(self, timeout=0):
            _ = timeout
            return "[[PDF_OUTPUT_READY]]\nResume PDF\nCover Letter PDF\n[[PDF_OUTPUT_COMPLETE]]"

    class _FakeRoleLocator:
        first = None

        async def count(self) -> int:
            return 0

    class _FakePage:
        context = _FakeContext()

        async def bring_to_front(self) -> None:
            return None

        def get_by_role(self, role: str, name: str):
            _ = (role, name)
            return _FakeRoleLocator()

    prompts: list[str] = []

    async def _fake_find(page, turn_locator, label):
        _ = (page, turn_locator, label)
        return turn_locator, _FakeLocator()

    async def _fake_wait(*args, **kwargs):
        _ = (args, kwargs)
        return None

    async def _fake_request_downloadable_attachments(page, *, reason):
        _ = (page, reason)
        prompts.append(reason)
        return {"locator": _FakeTurnLocator(), "text": "done"}

    monkeypatch.setattr(service, "_find_download_element_with_retry", _fake_find)
    monkeypatch.setattr(service, "_wait_for_new_pdf", _fake_wait)
    monkeypatch.setattr(service, "_request_downloadable_attachments", _fake_request_downloadable_attachments)
    monkeypatch.setattr(service, "_download_watch_dirs", lambda raw_root: [raw_root])
    monkeypatch.setattr(service, "_snapshot_download_entries", lambda directories: {})

    try:
        asyncio.run(
            service._download_pdfs(
                _FakePage(),
                _FakeTurnLocator(),
                raw_root,
                company="Acme",
                role="Backend Engineer",
                use_temporary_chat=True,
            )
        )
        raise AssertionError("Expected temporary-chat download failure to raise")
    except RuntimeError as exc:
        assert str(exc) == "download_timed_out:resume"

    assert prompts == []
