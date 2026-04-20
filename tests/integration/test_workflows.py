from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from findmyjob.apply.browser import PlaywrightSubmitter
from findmyjob.core.enums import (
    ApplicationMode,
    FactKind,
    JobLifecycleStatus,
    QuestionType,
    ReviewStatus,
    RunStatus,
    RunType,
    Sensitivity,
    TaskStatus,
    TaskType,
    WorkplaceType,
)
from findmyjob.core.config import SourceSettings
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import NormalizedJobPosting, ProfileFact, SubmissionEvidence, SubmissionResult
from findmyjob.db.models import SubmitAttemptRecord, utcnow
from findmyjob.db.repositories import ApplicationRepository, JobRepository, ProfileRepository, RunRepository, hash_content
from findmyjob.documents.pipeline import RenderedArtifact
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.sources.adapters.lever import LeverAdapter

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def fixture_json(name: str) -> dict | list:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def greenhouse_response(url: str, payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def configure_sources(runtime: AppRuntime) -> None:
    runtime.config.sources = {
        "greenhouse": SourceSettings(enabled=True, boards=["acme"], submit_enabled=True),
        "lever": SourceSettings(enabled=True, boards=["leverdemo-8"], submit_enabled=True),
        "ashby": SourceSettings(enabled=False, boards=[], submit_enabled=False),
    }


def seed_profile(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        repo.upsert_fact(
            ProfileFact(
                fact_id="contact-1",
                kind=FactKind.CONTACT,
                payload={
                    "name": "Test User",
                    "email": "user@example.com",
                    "phone": "555-0100",
                    "linkedin": "https://linkedin.com/in/test-user",
                },
                sensitivity=Sensitivity.LOW,
            )
        )
        repo.upsert_fact(
            ProfileFact(
                fact_id="auth-1",
                kind=FactKind.AUTHORIZATION,
                payload={"is_authorized": True, "requires_future_sponsorship": False},
                sensitivity=Sensitivity.HIGH,
            )
        )
        repo.upsert_fact(
            ProfileFact(
                fact_id="work-1",
                kind=FactKind.WORK,
                payload={"summary": "Built reliable APIs and durable background workflows for internal developer tooling."},
                sensitivity=Sensitivity.LOW,
            )
        )
        repo.upsert_fact(
            ProfileFact(
                fact_id="project-1",
                kind=FactKind.PROJECT,
                payload={"summary": "Shipped a job application preparation tool with structured evidence and resumable state."},
                sensitivity=Sensitivity.LOW,
            )
        )
        repo.upsert_fact(
            ProfileFact(
                fact_id="skill-1",
                kind=FactKind.SKILL,
                payload={"name": "Python", "summary": "Python"},
                sensitivity=Sensitivity.LOW,
            )
        )
        repo.upsert_fact(
            ProfileFact(
                fact_id="location-1",
                kind=FactKind.LOCATION,
                payload={"city": "San Francisco", "region_code": "CA", "country_code": "US", "display": "San Francisco, CA"},
                sensitivity=Sensitivity.LOW,
            )
        )


@pytest.fixture()
def runtime(tmp_path: Path) -> AppRuntime:
    rt = AppRuntime.bootstrap(tmp_path)
    configure_sources(rt)
    seed_profile(rt)
    return rt


@pytest.fixture()
def pdf_artifacts(monkeypatch):
    def fake_render(self, template_name: str, base_name: str, context: dict):
        suffix = template_name.replace(".typ", "")
        path = self.artifacts_dir / f"{base_name}.{suffix}.pdf"
        path.write_bytes(b"%PDF-1.4\n%stub\n")
        return RenderedArtifact(
            kind="pdf",
            path=path,
            content_hash=hash_content(path.read_bytes().hex()),
            validation_results={"valid": True, "page_count": 1, "one_page_ok": True, "contains_placeholder": False, "missing_contact_fields": [], "text_length": 128},
        )

    monkeypatch.setattr("findmyjob.documents.pipeline.DocumentPipeline.render_typst", fake_render)


@pytest.fixture()
def greenhouse_get(monkeypatch):
    discovery_payload = fixture_json("greenhouse_discovery_jobs.json")
    question_payload = fixture_json("greenhouse_job_questions.json")

    async def fake_get(self, url: str, params=None, **kwargs):
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return greenhouse_response(url, discovery_payload)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return greenhouse_response(url, question_payload)
        if url.startswith("https://api.lever.co/"):
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return fake_get


@pytest.mark.anyio
async def test_real_greenhouse_prepare_run_persists_artifacts_and_real_file_bindings(runtime: AppRuntime, greenhouse_get, pdf_artifacts) -> None:
    orchestrator = Orchestrator(runtime)
    await orchestrator.run_discovery(ApplicationMode.DRY_RUN)
    run_id = await orchestrator.run_prepare(ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED

        job = JobRepository(session).list_jobs(limit=1)[0]
        app_repo = ApplicationRepository(session)
        application = app_repo.get_application_for_job(job.id)
        assert application is not None
        assert application.status == JobLifecycleStatus.READY_FOR_REVIEW

        question_answers = app_repo.list_answers_for_application(application.id)
        by_field = {question.source_field_name: answer for question, answer in question_answers}
        assert by_field["email"].candidate_answer == "user@example.com"
        assert by_field["phone"].candidate_answer == "555-0100"
        assert by_field["resume"].candidate_answer != "ATTACH_ARTIFACT"
        assert Path(by_field["resume"].candidate_answer).exists()
        assert by_field["resume"].binding_payload["path"] == by_field["resume"].candidate_answer

        artifacts = app_repo.list_artifacts(application_id=application.id)
        kinds = {artifact.kind.value for artifact in artifacts}
        assert "review_packet" in kinds
        assert "resume_text" in kinds
        assert "cover_letter_text" in kinds

    plan = await orchestrator.inspect_submission_plan(job.id)
    assert plan is not None
    assert plan.missing_required_fields == []
    assert any(field.artifact_binding is not None for field in plan.fields)


@pytest.mark.anyio
async def test_real_greenhouse_apply_run_records_receipts_and_submission_plan(runtime: AppRuntime, greenhouse_get, monkeypatch, pdf_artifacts) -> None:
    submitted_plans = []

    async def fake_submit(self, url: str, plan, output_dir: Path):
        submitted_plans.append(plan)
        output_dir.mkdir(parents=True, exist_ok=True)
        pre_path = output_dir / "pre-submit.png"
        receipt_path = output_dir / "receipt.png"
        trace_path = output_dir / "trace.zip"
        dom_before = output_dir / "submit-dom-before.html"
        dom_after = output_dir / "submit-dom-after.html"
        pre_path.write_text("pre", encoding="utf-8")
        receipt_path.write_text("submitted", encoding="utf-8")
        trace_path.write_text("trace", encoding="utf-8")
        dom_before.write_text("<form></form>", encoding="utf-8")
        dom_after.write_text("<div>Application submitted</div>", encoding="utf-8")
        return SubmissionResult(
            status=JobLifecycleStatus.SUBMITTED,
            submitted=True,
            uncertain=False,
            message="Submitted",
            snapshot_path=str(receipt_path),
            trace_path=str(trace_path),
            plan=plan,
            evidence=SubmissionEvidence(
                pre_submit_snapshot_path=str(pre_path),
                final_snapshot_path=str(receipt_path),
                trace_path=str(trace_path),
                dom_snapshot_path=str(dom_before),
                post_submit_dom_snapshot_path=str(dom_after),
                confirmation_text="Application submitted",
                confirmation_strategy="explicit_success_text",
                matched_confirmation_markers=["text=/application submitted/i"],
                final_url="https://boards.greenhouse.io/acme/thank_you",
            ),
        )

    monkeypatch.setattr(PlaywrightSubmitter, "submit_greenhouse", fake_submit)

    orchestrator = Orchestrator(runtime)
    await orchestrator.run_discovery(ApplicationMode.DRY_RUN)
    await orchestrator.run_prepare(ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        job = JobRepository(session).list_jobs(limit=1)[0]
        application = ApplicationRepository(session).get_application_for_job(job.id)
        assert application is not None
        application_id = application.id

    await orchestrator.review_action(application_id, ReviewStatus.APPROVED)
    run_id = await orchestrator.run_apply(ApplicationMode.AUTO_SUBMIT)

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        app_repo = ApplicationRepository(session)
        application = app_repo.get_application(application_id)
        assert application is not None
        assert application.status == JobLifecycleStatus.SUBMITTED
        attempts = session.query(SubmitAttemptRecord).all()
        assert len(attempts) == 1
        receipt_kinds = {artifact.kind.value for artifact in app_repo.list_artifacts(application_id=application.id)}
        assert "submission_receipt" in receipt_kinds
        assert "submission_trace" in receipt_kinds
        assert "snapshot" in receipt_kinds

    inspection = await orchestrator.inspect_submission_result(application_id)
    assert inspection is not None
    assert inspection["confirmation_strategy"] == "explicit_success_text"
    assert inspection["final_url"] == "https://boards.greenhouse.io/acme/thank_you"
    assert inspection["post_submit_dom_snapshot_path"].endswith("submit-dom-after.html")

    assert submitted_plans
    assert any(field.artifact_binding is not None for field in submitted_plans[0].fields)


def insert_greenhouse_job(runtime: AppRuntime) -> str:
    job = NormalizedJobPosting(
        company_name="Acme",
        company_key="acme",
        title="Software Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="123456",
        posting_url="https://boards.greenhouse.io/acme/jobs/123456",
        apply_url="https://boards.greenhouse.io/acme/jobs/123456",
        location_raw="Remote - United States",
        location_normalized="remote united states",
        workplace_type=WorkplaceType.REMOTE,
        employment_type="full_time",
        compensation=None,
        description="Build APIs and reliable backend systems for distributed teams.",
        normalized_description="build apis and reliable backend systems for distributed teams",
        discovered_at=utcnow(),
        job_identity_key="identity-123456",
        duplicate_cluster_key="cluster-123456",
        lifecycle_status=JobLifecycleStatus.CANDIDATE,
        notes={"board": "acme"},
    )
    with runtime.session_scope() as session:
        return JobRepository(session).upsert_job(job, raw_payload={"id": 123456}).id


@pytest.mark.anyio
async def test_resume_run_reclaims_stale_prepare_task_with_real_greenhouse_adapter(runtime: AppRuntime, greenhouse_get, pdf_artifacts) -> None:
    job_id = insert_greenhouse_job(runtime)
    with runtime.session_scope() as session:
        run_repo = RunRepository(session)
        run = run_repo.create_run(RunType.PREPARE.value, ApplicationMode.DRY_RUN, checkpoint_state={})
        task = run_repo.enqueue_task(run.id, TaskType.PREPARE_APPLICATION.value, {"job_id": job_id}, idempotency_key=f"prepare:{job_id}")
        task.status = TaskStatus.RUNNING
        task.attempt_count = 1
        task.lease_owner = "stale-worker"
        task.lease_expires_at = utcnow() - timedelta(seconds=30)
        run_id = run.id
        task_id = task.id

    await Orchestrator(runtime).resume_run(run_id)

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        task = next(item for item in RunRepository(session).list_tasks(run_id) if item.id == task_id)
        assert task.status == TaskStatus.COMPLETED


@pytest.mark.anyio
async def test_prepare_run_marks_run_failed_after_terminal_question_contract_errors(runtime: AppRuntime, monkeypatch) -> None:
    insert_greenhouse_job(runtime)

    async def failing_get(self, url: str, params=None, **kwargs):
        raise httpx.ConnectError("questions endpoint unavailable", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", failing_get)
    run_id = await Orchestrator(runtime).run_prepare(ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED
        tasks = RunRepository(session).list_tasks(run_id)
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.FAILED_TERMINAL
        assert tasks[0].attempt_count == tasks[0].max_attempts


@pytest.mark.anyio
async def test_real_lever_contract_uses_recorded_public_form_fields(monkeypatch) -> None:
    field_rows = fixture_json("lever_form_fields.json")

    async def fake_inspect(self, url: str):
        return field_rows

    monkeypatch.setattr(PlaywrightSubmitter, "inspect_lever_form", fake_inspect)
    adapter = LeverAdapter(["leverdemo-8"])
    job = NormalizedJobPosting(
        company_name="Lever Demo",
        company_key="leverdemo-8",
        title="Backend Engineer",
        source="lever",
        source_kind="lever",
        source_job_id="1f61c800-ce6c-4ec4-86ae-933d1553acd1",
        posting_url="https://jobs.lever.co/leverdemo-8/1f61c800-ce6c-4ec4-86ae-933d1553acd1",
        apply_url="https://jobs.lever.co/leverdemo-8/1f61c800-ce6c-4ec4-86ae-933d1553acd1/apply",
        location_raw="Remote",
        location_normalized="remote",
        workplace_type=WorkplaceType.REMOTE,
        employment_type="full_time",
        compensation=None,
        description="Backend engineer role.",
        normalized_description="backend engineer role",
        discovered_at=utcnow(),
        job_identity_key="lever-1",
        duplicate_cluster_key="lever-1",
        lifecycle_status=JobLifecycleStatus.CANDIDATE,
        notes={"board": "leverdemo-8"},
    )

    async with httpx.AsyncClient() as client:
        result = await adapter.load_application_contract(client, job)

    by_name = {question.source_field_name: question for question in result.questions}
    assert by_name["resume"].question_type == QuestionType.FILE
    assert by_name["surveysResponses[sponsorship]"].question_type == QuestionType.BOOLEAN
    assert by_name["surveysResponses[sponsorship]"].options == ["Yes", "No"]



@pytest.mark.anyio
async def test_run_prepare_for_job_prepares_single_application(runtime: AppRuntime, greenhouse_get, pdf_artifacts) -> None:
    orchestrator = Orchestrator(runtime)
    await orchestrator.run_discovery(ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        job = JobRepository(session).list_jobs(limit=1)[0]

    run_id = await orchestrator.run_prepare_for_job(job.id, ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        application = ApplicationRepository(session).get_application_for_job(job.id)
        assert application is not None
        assert application.status == JobLifecycleStatus.READY_FOR_REVIEW


@pytest.mark.anyio
async def test_run_apply_for_application_submits_single_application(runtime: AppRuntime, greenhouse_get, monkeypatch, pdf_artifacts) -> None:
    async def fake_submit(self, url: str, plan, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = output_dir / "receipt.png"
        receipt_path.write_text("submitted", encoding="utf-8")
        return SubmissionResult(
            status=JobLifecycleStatus.SUBMITTED,
            submitted=True,
            message="Submitted",
            snapshot_path=str(receipt_path),
            plan=plan,
            evidence=SubmissionEvidence(final_snapshot_path=str(receipt_path), confirmation_text="Application submitted"),
        )

    monkeypatch.setattr(PlaywrightSubmitter, "submit_greenhouse", fake_submit)

    orchestrator = Orchestrator(runtime)
    await orchestrator.run_discovery(ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        job = JobRepository(session).list_jobs(limit=1)[0]

    await orchestrator.run_prepare_for_job(job.id, ApplicationMode.DRY_RUN)

    with runtime.session_scope() as session:
        application = ApplicationRepository(session).get_application_for_job(job.id)
        assert application is not None
        application_id = application.id

    await orchestrator.review_action(application_id, ReviewStatus.APPROVED)
    run_id = await orchestrator.run_apply_for_application(application_id, ApplicationMode.AUTO_SUBMIT)

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        application = ApplicationRepository(session).get_application(application_id)
        assert application is not None
        assert application.status == JobLifecycleStatus.SUBMITTED
