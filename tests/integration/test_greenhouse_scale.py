from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from findmyjob.core.config import SourceSettings
from findmyjob.core.enums import FactKind, JobLifecycleStatus, LocationScope, RunStatus, Sensitivity, TaskStatus
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery, ProfileFact
from findmyjob.db.board_repository import BoardRepository, SourceStateRepository
from findmyjob.db.repositories import JobRepository, ProfileRepository, RunRepository
from findmyjob.db.search import search_jobs
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def seed_profile(runtime: AppRuntime) -> None:
    with runtime.session_scope() as session:
        repo = ProfileRepository(session)
        repo.upsert_fact(ProfileFact(fact_id="auth-1", kind=FactKind.AUTHORIZATION, payload={"is_authorized": True, "requires_future_sponsorship": False}, sensitivity=Sensitivity.HIGH))
        repo.upsert_fact(ProfileFact(fact_id="work-1", kind=FactKind.WORK, payload={"summary": "Built reliable APIs and background workflows."}, sensitivity=Sensitivity.LOW))
        repo.upsert_fact(ProfileFact(fact_id="skill-1", kind=FactKind.SKILL, payload={"name": "Python", "summary": "Python"}, sensitivity=Sensitivity.LOW))


@pytest.fixture()
def runtime(tmp_path: Path) -> AppRuntime:
    rt = AppRuntime.bootstrap(tmp_path)
    rt.config.sources = {
        "greenhouse": SourceSettings(
            enabled=True,
            boards=["acme"],
            seed_urls=["https://example.com/careers"],
            seed_domains=["example.com"],
            concurrency=2,
            max_boards_per_run=10,
            max_jobs_per_run=20,
            max_job_enrichment_tasks_per_run=20,
            stale_job_threshold=1,
            use_builtin_board_universe=False,
        )
    }
    seed_profile(rt)
    return rt


def response(url: str, *, payload: dict | None = None, text: str | None = None, status_code: int = 200) -> httpx.Response:
    kwargs = {"request": httpx.Request("GET", url), "status_code": status_code}
    if payload is not None:
        kwargs["json"] = payload
    if text is not None:
        kwargs["text"] = text
    return httpx.Response(**kwargs)


@pytest.mark.anyio
async def test_greenhouse_board_discovery_and_sync_creates_searchable_jobs(runtime: AppRuntime, monkeypatch) -> None:
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    detail_payload = fixture_json("greenhouse_job_questions.json")

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://example.com/sitemap.xml":
            return response(url, text="<urlset><url><loc>https://example.com/careers</loc></url></urlset>")
        if url in {"https://example.com", "https://example.com/careers"}:
            return response(url, text='<html><a href="https://boards.greenhouse.io/acme">Careers</a></html>')
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"name": "Acme"})
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=jobs_payload)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    orchestrator = GreenhouseScaleOrchestrator(runtime)
    await orchestrator.discover_boards()
    await orchestrator.sync_boards()

    with runtime.session_scope() as session:
        boards = BoardRepository(session).list_boards("greenhouse")
        assert [board.board_token for board in boards] == ["acme"]
        jobs = JobRepository(session).list_jobs(limit=10)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.board_token == "acme"
        assert job.country_code == "US"
        assert job.location_scope == LocationScope.REMOTE_US.value
        assert job.posted_at is not None
        assert job.compensation_min == 120000
        assert job.compensation_max == 150000
        assert job.compensation_currency == "USD"
        assert job.notes.get("question_count") == len(detail_payload.get("questions") or [])
        latest_run = RunRepository(session).list_runs(limit=1)[0]
        assert latest_run.checkpoint_state.get("boards_processed", 0) >= 1
        assert latest_run.checkpoint_state.get("jobs_seen", 0) >= 1
        matches = search_jobs(session, JobSearchQuery(keyword="engineer", source_adapter="greenhouse", active_only=True, countries=["US"], remote_only=True, posted_within_days=365, compensation_min=100000, limit=10))
        assert len(matches) == 1


@pytest.mark.anyio
async def test_greenhouse_sync_persists_inferred_experience_when_derivable(runtime: AppRuntime, monkeypatch) -> None:
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    jobs_payload["jobs"][0]["title"] = "Senior Software Engineer"
    detail_payload = fixture_json("greenhouse_job_questions.json")
    detail_payload["title"] = "Senior Software Engineer"

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"name": "Acme"})
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=jobs_payload)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await GreenhouseScaleOrchestrator(runtime).sync_boards()

    with runtime.session_scope() as session:
        job = JobRepository(session).list_jobs(limit=1)[0]
        assert job.experience_level == "senior"


@pytest.mark.anyio
async def test_greenhouse_sync_marks_missing_jobs_inactive(runtime: AppRuntime, monkeypatch) -> None:
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    detail_payload = fixture_json("greenhouse_job_questions.json")
    state = {"jobs": jobs_payload}

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"name": "Acme"})
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=state["jobs"])
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    orchestrator = GreenhouseScaleOrchestrator(runtime)
    await orchestrator.sync_boards()
    state["jobs"] = {"jobs": []}
    await orchestrator.sync_boards()

    with runtime.session_scope() as session:
        jobs = JobRepository(session).list_jobs(limit=10)
        assert len(jobs) == 1
        assert jobs[0].lifecycle_status == JobLifecycleStatus.INACTIVE
        matches = search_jobs(session, JobSearchQuery(keyword="engineer", source_adapter="greenhouse", active_only=True, limit=10))
        assert matches == []


@pytest.mark.anyio
async def test_greenhouse_sync_records_backoff_after_validation_failure(runtime: AppRuntime, monkeypatch) -> None:
    async def failing_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        raise httpx.ConnectError("board validation unavailable", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", failing_get)

    run_id = await GreenhouseScaleOrchestrator(runtime).sync_boards()

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.checkpoint_state.get("request_count", 0) >= 1
        assert "backoff_boards" in run.checkpoint_state
        board = BoardRepository(session).get_board("greenhouse", "acme")
        assert board is not None
        assert board.validation_status in {"invalid", "unknown", "inactive"}
        state = SourceStateRepository(session).get_board_sync_state("greenhouse", "acme")
        assert state.backoff_until is not None

@pytest.mark.anyio
async def test_greenhouse_benchmark_persists_summary_metrics(runtime: AppRuntime, monkeypatch) -> None:
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    detail_payload = fixture_json("greenhouse_job_questions.json")

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"name": "Acme"})
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=jobs_payload)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    orchestrator = GreenhouseScaleOrchestrator(runtime)
    summary = await orchestrator.benchmark(max_boards=1)

    assert summary.status == RunStatus.COMPLETED.value
    assert summary.boards_attempted == 1
    assert summary.boards_succeeded == 1
    assert summary.jobs_seen == 1
    assert summary.jobs_enriched == 1
    assert summary.request_count >= 3
    assert summary.duration_seconds >= 0

    history = orchestrator.list_benchmarks(limit=1)
    assert len(history) == 1
    assert history[0].run_id == summary.run_id
    assert history[0].jobs_seen == 1


@pytest.mark.anyio
async def test_greenhouse_sync_accepts_list_shaped_board_and_jobs_payloads(runtime: AppRuntime, monkeypatch) -> None:
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    detail_payload = fixture_json("greenhouse_job_questions.json")

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload=[{"name": "Acme"}])
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=jobs_payload["jobs"])
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    run_id = await GreenhouseScaleOrchestrator(runtime).sync_boards()

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        board = BoardRepository(session).get_board("greenhouse", "acme")
        assert board is not None
        assert board.company_hint == "Acme"
        jobs = JobRepository(session).list_jobs(limit=10)
        assert len(jobs) == 1
        assert jobs[0].board_token == "acme"


@pytest.mark.anyio
async def test_greenhouse_sync_tolerates_list_shaped_nested_metadata(runtime: AppRuntime, monkeypatch) -> None:
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    jobs_payload["jobs"][0]["metadata"] = []
    detail_payload = fixture_json("greenhouse_job_questions.json")
    detail_payload["metadata"] = []

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"name": "Acme"})
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=jobs_payload)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    run_id = await GreenhouseScaleOrchestrator(runtime).sync_boards()

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        jobs = JobRepository(session).list_jobs(limit=10)
        assert len(jobs) == 1
        assert jobs[0].employment_type in {None, ""}


@pytest.mark.anyio
async def test_greenhouse_sync_marks_missing_board_invalid_without_failed_task(runtime: AppRuntime, monkeypatch) -> None:
    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return response(url, text="User-agent: *\nAllow: /")
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"error": "not found"}, status_code=404)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    run_id = await GreenhouseScaleOrchestrator(runtime).sync_boards()

    with runtime.session_scope() as session:
        run_repo = RunRepository(session)
        run = run_repo.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        assert run.checkpoint_state.get("failed_tasks") == 0
        assert run.checkpoint_state.get("boards_succeeded") == 0
        assert run.checkpoint_state.get("boards_skipped") == 1
        tasks = list(run_repo.list_tasks(run_id))
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.COMPLETED
        assert tasks[0].checkpoint_state.get("reason") == "board_not_found"
        board = BoardRepository(session).get_board("greenhouse", "acme")
        assert board is not None
        assert board.validation_status == "invalid"
        assert board.last_sync_status == "skipped_invalid"
        assert board.active is False
        state = SourceStateRepository(session).get_board_sync_state("greenhouse", "acme")
        assert state.last_validation_result == "invalid"
        assert state.backoff_until is None

