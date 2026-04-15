"""Tests for the training run orchestration and feedback."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from findmyjob.apply.greenhouse_training import VALID_POSTED_WINDOWS
from findmyjob.core.enums import ApplicationMode, FactKind, QuestionType, Sensitivity, VerificationStatus
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import (
    ArtifactDraft,
    CoverLetterDraft,
    GroundedAnswer,
    ProfileFact,
    ResumeDraft,
    TrainingPageCapture,
    TrainingReviewOutcome,
    TrainingRunSummary,
)
from findmyjob.db.repositories import ApplicationRepository, ProfileRepository, RunRepository
from findmyjob.personal.training import (
    TrainingModeError,
    build_training_report,
    generate_training_draft,
    list_training_history,
    list_training_review_history,
    list_training_runs,
    load_prior_training_feedback,
    persist_training_feedback,
    promote_training_samples,
    run_greenhouse_training,
)


class TestTrainingRunSummary:
    def test_defaults(self):
        summary = TrainingRunSummary(run_id="abc123")
        assert summary.posted_window == 10
        assert summary.batch_size == 5
        assert summary.cdp_url == "http://127.0.0.1:9222"
        assert summary.start_url == "https://my.greenhouse.io/jobs"
        assert summary.sampled_jobs == []
        assert summary.reviews == []
        assert summary.approved_count == 0
        assert summary.rejected_count == 0

    def test_review_outcome_is_approve_reject_only(self):
        fields = TrainingReviewOutcome.model_fields
        assert "approved" in fields
        assert "submitted" not in fields


class TestPostedWindowValidation:
    def test_valid_windows(self):
        assert set(VALID_POSTED_WINDOWS) == {1, 5, 10, 30}


def _runtime(tmp_path: Path) -> AppRuntime:
    return AppRuntime.bootstrap(tmp_path)


def _seed_profile(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        repo.upsert_fact(
            ProfileFact(
                fact_id="contact.primary",
                kind=FactKind.CONTACT,
                payload={"name": "Test User", "email": "user@example.com"},
                sensitivity=Sensitivity.LOW,
            )
        )


def _write_text(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _inspection_payload(tmp_path: Path, *, view_url: str, company_url: str | None, apply_url: str | None) -> dict[str, object]:
    captures = [
        TrainingPageCapture(
            stage="job_view",
            url=view_url,
            page_title="My Greenhouse",
            screenshot_path=_write_text(tmp_path / "job_view.png", "job_view"),
            dom_snapshot_path=_write_text(tmp_path / "job_view.html", "<main>Short description</main>"),
            job_description_text="Short description",
        ),
    ]
    if company_url:
        captures.append(
            TrainingPageCapture(
                stage="company_page",
                url=company_url,
                page_title="Company Job",
                screenshot_path=_write_text(tmp_path / "company_page.png", "company_page"),
                dom_snapshot_path=_write_text(tmp_path / "company_page.html", "<main>Longer company description</main>"),
                job_description_text="Longer company description",
            )
        )
    fields = [
        {"name": "email", "label": "Email", "type": "text", "required": True},
        {"name": "resume", "label": "Resume", "type": "file", "required": True},
    ]
    if apply_url:
        captures.append(
            TrainingPageCapture(
                stage="apply_page",
                url=apply_url,
                page_title="Apply",
                screenshot_path=_write_text(tmp_path / "apply_page.png", "apply_page"),
                dom_snapshot_path=_write_text(tmp_path / "apply_page.html", "<form><input name='email'></form>"),
                extracted_fields=fields,
                job_description_text="Longer company description",
                layout_notes=["Apply form opened via click control."],
            )
        )
    return {
        "page_captures": captures,
        "company_page_url": company_url,
        "apply_url": apply_url,
        "job_description_text": "Longer company description",
        "form_fields": fields if apply_url else [],
        "screenshot_paths": [capture.screenshot_path for capture in captures if capture.screenshot_path],
        "dom_snapshot_paths": [capture.dom_snapshot_path for capture in captures if capture.dom_snapshot_path],
        "layout_notes": ["Apply form opened via click control."] if apply_url else ["No apply control found on the current page."],
    }


class TestPersistTrainingFeedback:
    def test_feedback_emitted_and_loaded(self, tmp_path: Path) -> None:
        rt = _runtime(tmp_path)
        review = TrainingReviewOutcome(
            job_url="https://my.greenhouse.io/jobs/1",
            job_title="SWE",
            company_name="Acme",
            approved=False,
            rejection_note="Needs better evidence",
            rejection_reason_code="missing_evidence",
            feedback_summary="Rejected: Missing evidence. Needs better evidence",
            linked_application_id="app-1",
            reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        with rt.session_scope() as session:
            run_id = RunRepository(session).create_run("training", ApplicationMode.DRY_RUN, checkpoint_state={}).id
        persist_training_feedback(rt, review, run_id=run_id)
        feedback = load_prior_training_feedback(rt, limit=10)

        assert len(feedback) == 1
        assert feedback[0]["approved"] is False
        assert feedback[0]["rejection_reason_code"] == "missing_evidence"
        assert feedback[0]["linked_application_id"] == "app-1"

        history = list_training_review_history(rt, limit=10)
        assert len(history) == 1
        assert history[0]["run_id"] == run_id
        assert history[0]["rejection_reason_code"] == "missing_evidence"


class TestGenerateTrainingDraft:
    @pytest.mark.anyio
    async def test_no_writer_raises(self):
        rt = MagicMock()
        rt.model_router.get_profile.side_effect = ValueError("No writer")
        with pytest.raises(TrainingModeError, match="No writer model profile"):
            await generate_training_draft(rt, "desc", "SWE", "Acme")

    @pytest.mark.anyio
    async def test_successful_draft_includes_feedback_context(self):
        rt = MagicMock()
        writer_mock = MagicMock()
        writer_mock.name = "prism-writer"
        rt.model_router.get_profile.return_value = writer_mock
        rt.model_router.generate_json = AsyncMock(
            return_value={
                "resume_draft": {
                    "headline": "Senior SWE",
                    "summary_lines": ["Experienced engineer"],
                    "selected_work_fact_ids": ["w1"],
                },
                "cover_letter_draft": {
                    "salutation": "Dear Hiring Manager",
                    "paragraphs": ["I am excited...", "My experience..."],
                    "closing": "Best regards",
                    "signature_name": "Jane Doe",
                },
                "adaptation_summary": "Shifted emphasis toward quantified backend delivery and removed unsupported claims.",
            }
        )

        @contextmanager
        def mock_scope():
            yield MagicMock()

        rt.session_scope = mock_scope
        mock_repo = MagicMock()
        mock_repo.list_facts.return_value = []
        prior_feedback = [
            {
                "approved": False,
                "job_title": "Backend Engineer",
                "company_name": "Acme",
                "rejection_reason_code": "missing_evidence",
                "feedback_summary": "Rejected: Missing evidence.",
            }
        ]

        with patch("findmyjob.db.repositories.ProfileRepository", return_value=mock_repo):
            draft = await generate_training_draft(rt, "Build APIs", "Backend Engineer", "Acme", prior_feedback=prior_feedback)

        assert draft.resume_draft.headline == "Senior SWE"
        assert len(draft.cover_letter_draft.paragraphs) == 2
        assert draft.adaptation_summary is not None
        assert draft.feedback_context
        prompt = rt.model_router.generate_json.await_args.args[1]
        assert "Prior training feedback" in prompt
        assert "Missing evidence" in prompt


class FakeTrainingPage:
    def __init__(self, url: str = "https://my.greenhouse.io/jobs"):
        self.url = url
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_load_state(self, state: str, **kwargs) -> None:
        pass


class TestRunGreenhouseTraining:
    @pytest.mark.anyio
    async def test_traverses_company_page_before_apply_and_promotes_approved_sample(self, tmp_path: Path):
        rt = _runtime(tmp_path)
        _seed_profile(rt)
        page = FakeTrainingPage()
        cdp_calls: list[dict[str, object]] = []
        inspection = _inspection_payload(
            tmp_path,
            view_url="https://my.greenhouse.io/jobs/123",
            company_url="https://boards.greenhouse.io/acme/jobs/123",
            apply_url="https://boards.greenhouse.io/acme/jobs/123#application",
        )
        resume_path = _write_text(tmp_path / "resume.pdf", "resume")
        cover_path = _write_text(tmp_path / "cover.pdf", "cover")
        resume_text_path = _write_text(tmp_path / "resume.txt", "resume text")
        cover_text_path = _write_text(tmp_path / "cover.txt", "cover text")

        @asynccontextmanager
        async def fake_cdp_browser_context(cdp_url: str, *, keep_tabs_open: bool = False):
            cdp_calls.append({"cdp_url": cdp_url, "keep_tabs_open": keep_tabs_open})
            yield object(), object()

        async def fake_answer(prompt_text: str, facts, **kwargs):
            return GroundedAnswer(
                question=prompt_text,
                question_type=QuestionType.DETERMINISTIC,
                answer="user@example.com",
                canonical_question="email",
                verification_status=VerificationStatus.VERIFIED,
            )

        with (
            patch("findmyjob.personal.training._ensure_training_ready", return_value=None),
            patch("findmyjob.personal.training.cdp_browser_context", fake_cdp_browser_context),
            patch("findmyjob.personal.training.find_or_open_tab", AsyncMock(return_value=page)),
            patch("findmyjob.personal.training.set_posted_window", AsyncMock()),
            patch(
                "findmyjob.personal.training.harvest_visible_jobs",
                AsyncMock(
                    return_value=[
                        {
                            "url": "https://my.greenhouse.io/jobs/123",
                            "title": "Backend Engineer",
                            "company": "Acme",
                            "location": "Remote",
                            "posted_text": "2 days ago",
                        }
                    ]
                ),
            ),
            patch("findmyjob.personal.training.inspect_training_job_path", AsyncMock(return_value=inspection)),
            patch(
                "findmyjob.personal.training.generate_training_draft",
                AsyncMock(
                    return_value=ArtifactDraft(
                        resume_draft=ResumeDraft(),
                        cover_letter_draft=CoverLetterDraft(),
                        adaptation_summary="Updated toward measurable backend work.",
                    )
                ),
            ),
            patch(
                "findmyjob.personal.training.render_training_artifacts",
                return_value={
                    "resume_pdf_path": resume_path,
                    "resume_text_path": resume_text_path,
                    "cover_letter_pdf_path": cover_path,
                    "cover_letter_text_path": cover_text_path,
                },
            ),
            patch.object(rt.grounding, "answer_question", AsyncMock(side_effect=fake_answer)),
            patch.object(rt.grounding, "classify_question", return_value=QuestionType.DETERMINISTIC),
        ):
            summary = await run_greenhouse_training(rt, keep_tabs_open=True, prompt_fn=lambda job, artifacts: (True, None, None))

        assert cdp_calls == [{"cdp_url": "http://127.0.0.1:9222", "keep_tabs_open": True}]
        assert len(summary.sampled_jobs) == 1
        job_summary = summary.sampled_jobs[0]
        assert job_summary.company_page_url == "https://boards.greenhouse.io/acme/jobs/123"
        assert job_summary.apply_url == "https://boards.greenhouse.io/acme/jobs/123#application"
        assert job_summary.linked_application_id is not None
        assert job_summary.review_packet_path is not None
        assert job_summary.draft_change_summary == "Updated toward measurable backend work."
        assert summary.promoted_application_ids == [job_summary.linked_application_id]

        with rt.session_scope() as session:
            app_repo = ApplicationRepository(session)
            application = app_repo.get_application(job_summary.linked_application_id)
            assert application is not None
            assert application.status.value == "ready_for_review"
            assert application.review_status.value == "pending"
            assert app_repo.list_questions(application.id)
            artifacts = list(app_repo.list_artifacts(application_id=application.id))
            kinds = {artifact.kind.value for artifact in artifacts}
            assert {"review_packet", "resume_pdf", "resume_text", "cover_letter_pdf", "cover_letter_text"}.issubset(kinds)

        history = list_training_history(rt, run_id=summary.run_id)
        assert len(history) == 1
        sample = history[0]
        assert sample.company_page_url == job_summary.company_page_url
        assert sample.apply_page_url == job_summary.apply_url
        assert sample.extracted_form_fields[0]["name"] == "email"
        assert sample.promoted_application_id == job_summary.linked_application_id
        assert sample.review_packet_path == job_summary.review_packet_path

        review_packet = json.loads(Path(job_summary.review_packet_path).read_text(encoding="utf-8"))
        assert review_packet["submit_ready"] is False
        assert review_packet["handoff_url"] == job_summary.apply_url

        rerun = await promote_training_samples(rt, sample_id=sample.sample_id)
        assert rerun[0].promoted is True
        assert rerun[0].application_id == job_summary.linked_application_id
        assert rerun[0].notes == ["Sample was already promoted."]

        runs = list_training_runs(rt, limit=5)
        assert runs[0]["run_id"] == summary.run_id
        report = build_training_report(rt, run_id=summary.run_id)
        assert report["approved_count"] == 1
        assert report["promoted_application_ids"] == [job_summary.linked_application_id]
        assert report["health"]["with_company_page_url"] == 1
        assert report["health"]["with_apply_url"] == 1

    @pytest.mark.anyio
    async def test_persists_structured_rejection_feedback(self, tmp_path: Path):
        rt = _runtime(tmp_path)
        page = FakeTrainingPage()
        inspection = _inspection_payload(
            tmp_path,
            view_url="https://my.greenhouse.io/jobs/456",
            company_url="https://boards.greenhouse.io/acme/jobs/456",
            apply_url="https://boards.greenhouse.io/acme/jobs/456#application",
        )

        @asynccontextmanager
        async def fake_cdp_browser_context(cdp_url: str, *, keep_tabs_open: bool = False):
            yield object(), object()

        with (
            patch("findmyjob.personal.training._ensure_training_ready", return_value=None),
            patch("findmyjob.personal.training.cdp_browser_context", fake_cdp_browser_context),
            patch("findmyjob.personal.training.find_or_open_tab", AsyncMock(return_value=page)),
            patch("findmyjob.personal.training.set_posted_window", AsyncMock()),
            patch(
                "findmyjob.personal.training.harvest_visible_jobs",
                AsyncMock(
                    return_value=[
                        {
                            "url": "https://my.greenhouse.io/jobs/456",
                            "title": "Backend Engineer",
                            "company": "Acme",
                            "location": "Remote",
                            "posted_text": "2 days ago",
                        }
                    ]
                ),
            ),
            patch("findmyjob.personal.training.inspect_training_job_path", AsyncMock(return_value=inspection)),
            patch(
                "findmyjob.personal.training.generate_training_draft",
                AsyncMock(return_value=ArtifactDraft(resume_draft=ResumeDraft(), cover_letter_draft=CoverLetterDraft())),
            ),
            patch("findmyjob.personal.training.render_training_artifacts", return_value={}),
        ):
            summary = await run_greenhouse_training(
                rt,
                prompt_fn=lambda job, artifacts: (False, "missing_evidence", "Need quantified backend examples."),
            )

        assert summary.approved_count == 0
        assert summary.rejected_count == 1
        history = list_training_history(rt, run_id=summary.run_id)
        assert len(history) == 1
        sample = history[0]
        assert sample.review_status == "rejected"
        assert sample.review_reason_code == "missing_evidence"
        assert sample.review_note == "Need quantified backend examples."
        assert sample.promoted_application_id is None

        review_history = list_training_review_history(rt, run_id=summary.run_id)
        assert review_history[0]["rejection_reason_code"] == "missing_evidence"
        assert review_history[0]["approved"] is False

    @pytest.mark.anyio
    async def test_uses_stable_sample_order(self, tmp_path: Path):
        rt = _runtime(tmp_path)
        page = FakeTrainingPage()

        @asynccontextmanager
        async def fake_cdp_browser_context(cdp_url: str, *, keep_tabs_open: bool = False):
            yield object(), object()

        async def fake_inspection(page, job_data, output_dir):
            return _inspection_payload(
                tmp_path / (job_data["title"].replace(" ", "_").lower()),
                view_url=job_data["url"],
                company_url=None,
                apply_url=None,
            )

        with (
            patch("findmyjob.personal.training._ensure_training_ready", return_value=None),
            patch("findmyjob.personal.training.cdp_browser_context", fake_cdp_browser_context),
            patch("findmyjob.personal.training.find_or_open_tab", AsyncMock(return_value=page)),
            patch("findmyjob.personal.training.set_posted_window", AsyncMock()),
            patch(
                "findmyjob.personal.training.harvest_visible_jobs",
                AsyncMock(
                    return_value=[
                        {"url": "https://my.greenhouse.io/jobs/2", "title": "Zeta Engineer", "company": "Zulu", "posted_text": "2 days ago"},
                        {"url": "https://my.greenhouse.io/jobs/1", "title": "Alpha Engineer", "company": "Acme", "posted_text": "2 days ago"},
                    ]
                ),
            ),
            patch("findmyjob.personal.training.inspect_training_job_path", AsyncMock(side_effect=fake_inspection)),
            patch("findmyjob.personal.training.generate_training_draft", AsyncMock(return_value=ArtifactDraft())),
            patch("findmyjob.personal.training.render_training_artifacts", return_value={}),
        ):
            summary = await run_greenhouse_training(rt, batch_size=1)

        assert [job.job_url for job in summary.sampled_jobs] == ["https://my.greenhouse.io/jobs/1"]
        history = list_training_history(rt, run_id=summary.run_id)
        assert history[0].review_reason_code == "noninteractive_default"
