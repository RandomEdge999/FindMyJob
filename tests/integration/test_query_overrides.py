from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from findmyjob.core.config import SourceSettings
from findmyjob.core.enums import ApplicationMode, JobLifecycleStatus, PolicyMode, SourceRisk
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery, NormalizedJobPosting, SourceCapabilities
from findmyjob.db.repositories import JobRepository, RunRepository
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.sources.base import SourceAdapter
from findmyjob.sources.normalizer import build_normalized_job

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"



def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))



def response(url: str, *, payload: dict | None = None, text: str | None = None, status_code: int = 200) -> httpx.Response:
    kwargs = {"request": httpx.Request("GET", url), "status_code": status_code}
    if payload is not None:
        kwargs["json"] = payload
    if text is not None:
        kwargs["text"] = text
    return httpx.Response(**kwargs)


class FakeAdapter(SourceAdapter):
    adapter_name = "fake"

    def __init__(self, seen: dict[str, object]) -> None:
        super().__init__(["fake-board"])
        self.seen = seen

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            adapter_name=self.adapter_name,
            source_kind="fake",
            policy_mode=PolicyMode.PUBLIC_READ_ONLY,
            risk=SourceRisk.LOW,
        )

    async def discover(self, client: httpx.AsyncClient, query) -> list[tuple[NormalizedJobPosting, dict]]:
        self.seen["query"] = query.model_dump(mode="json")
        posting = build_normalized_job(
            company_name="Acme",
            title="Backend Engineer",
            source="fake",
            source_kind="fake",
            source_job_id="1",
            posting_url="https://example.com/jobs/1",
            apply_url="https://example.com/jobs/1",
            location_raw="Remote - United States",
            employment_type="full_time",
            compensation=None,
            description="Backend engineering role.",
        )
        return [(posting, {"id": 1})]


@pytest.mark.anyio
async def test_orchestrator_run_discovery_uses_explicit_query_override(tmp_path: Path, monkeypatch) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    seen: dict[str, object] = {}
    orchestrator = Orchestrator(runtime)
    monkeypatch.setattr(orchestrator, "source_adapters", lambda query_override=None: {"fake": FakeAdapter(seen)})

    run_id = await orchestrator.run_discovery(
        ApplicationMode.DRY_RUN,
        None,
        JobSearchQuery(title_keywords=["platform"], countries=["US"], remote_only=True),
    )

    with runtime.session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        jobs = JobRepository(session).list_jobs(limit=5)
        assert seen["query"]["title_keywords"] == ["platform"]
        assert run is not None
        assert run.checkpoint_state["query"]["title_keywords"] == ["platform"]
        assert len(jobs) == 1
        assert jobs[0].lifecycle_status == JobLifecycleStatus.SCREENED_OUT


@pytest.mark.anyio
async def test_greenhouse_sync_uses_explicit_query_override(tmp_path: Path, monkeypatch) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    runtime.config.sources = {
        "greenhouse": SourceSettings(
            enabled=True,
            boards=["acme"],
            concurrency=1,
            max_boards_per_run=10,
            max_jobs_per_run=20,
            max_job_enrichment_tasks_per_run=20,
            stale_job_threshold=1,
        )
    }
    jobs_payload = fixture_json("greenhouse_discovery_jobs.json")
    detail_payload = fixture_json("greenhouse_job_questions.json")

    async def fake_get(self, url: str, params=None, timeout=None, **kwargs):
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return response(url, payload={"name": "Acme"})
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return response(url, payload=jobs_payload)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456":
            return response(url, payload=detail_payload)
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    await GreenhouseScaleOrchestrator(runtime).sync_boards(query_override=JobSearchQuery(title_keywords=["designer"], source_adapter="greenhouse"))

    with runtime.session_scope() as session:
        jobs = JobRepository(session).list_jobs(limit=5)
        assert len(jobs) == 1
        assert jobs[0].lifecycle_status == JobLifecycleStatus.SCREENED_OUT
