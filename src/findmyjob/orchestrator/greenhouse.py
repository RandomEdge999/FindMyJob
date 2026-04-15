
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import anyio
import httpx
from sqlalchemy import select

from findmyjob.core.config import SourceSettings
from findmyjob.core.enums import ApplicationMode, JobLifecycleStatus, RunStatus, RunType, TaskStatus, TaskType
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import BoardDiscoveryEvidence, BoardRegistry, BoardSyncState, GreenhouseBenchmarkSummary, JobSearchQuery
from findmyjob.db.board_repository import BoardRepository, SourceStateRepository
from findmyjob.db.models import JobPosting, utcnow
from findmyjob.db.repositories import AuditRepository, JobRepository, ProfileRepository, RunRepository, hash_content
from findmyjob.qualification.rules import qualification_for_job
from findmyjob.sources.contracts import DiscoveryQuery
from findmyjob.sources.greenhouse_scale import GreenhouseScaleClient
from findmyjob.sources.greenhouse_universe import builtin_greenhouse_board_tokens
from findmyjob.sources.normalizer import build_normalized_job, extract_posted_at, extract_source_updated_at


class GreenhouseScaleOrchestrator:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime
        self.settings = runtime.config.sources.get("greenhouse") or SourceSettings()
        self.scale = GreenhouseScaleClient(
            request_timeout_seconds=self.settings.request_timeout_seconds,
            requests_per_second=self.settings.per_host_request_rate,
            crawl_depth=self.settings.crawl_depth,
        )
        self._claim_lock = anyio.Lock()
        self._budget_lock = anyio.Lock()
        self._metrics_lock = anyio.Lock()
        self._db_write_lock = anyio.Lock()
        self._board_locks: dict[str, anyio.Lock] = {}
        self._remaining_jobs = self.settings.max_jobs_per_run
        self._remaining_enrichments = self.settings.max_job_enrichment_tasks_per_run
        self._run_metrics = {
            "boards_processed": 0,
            "jobs_seen": 0,
            "enriched_jobs": 0,
            "inactive_jobs": 0,
            "backoff_boards": 0,
        }

    def build_query(self, query_override: JobSearchQuery | None = None) -> DiscoveryQuery:
        if query_override is not None:
            return query_override.to_discovery_query()
        return JobSearchQuery.from_search_settings(self.runtime.config.search).to_discovery_query()

    async def discover_boards(self, resume_run_id: str | None = None) -> str:
        self.scale.reset_stats()
        self._reset_run_metrics()
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            if resume_run_id:
                run = run_repo.get_run(resume_run_id)
                if run is None:
                    raise ValueError(f"Run not found: {resume_run_id}")
                run_repo.reap_stale_leases()
            else:
                run = run_repo.create_run(RunType.DISCOVER_BOARDS.value, ApplicationMode.DRY_RUN, checkpoint_state={})
                run_repo.enqueue_task(
                    run.id,
                    TaskType.DISCOVER_BOARDS.value,
                    {"seed_urls": list(self.settings.seed_urls), "seed_domains": list(self.settings.seed_domains)},
                    idempotency_key="greenhouse:discover-boards",
                )
                audit_repo.emit("run.started", "run", run.id, run_id=run.id, payload={"type": RunType.DISCOVER_BOARDS.value})
        await self._process_run(run.id)
        return run.id

    async def sync_boards(
        self,
        resume_run_id: str | None = None,
        query_override: JobSearchQuery | None = None,
        *,
        board_tokens: list[str] | None = None,
        max_boards: int | None = None,
        run_type: RunType = RunType.SYNC,
    ) -> str:
        self.scale.reset_stats()
        self._reset_run_metrics()
        with self.runtime.session_scope() as session:
            board_repo = BoardRepository(session)
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            board_filter = query_override.board_token if query_override else None
            requested_tokens = list(dict.fromkeys([*(board_tokens or []), *([board_filter] if board_filter else [])]))
            seed_boards = requested_tokens or list(self.settings.boards)
            using_builtin_board_universe = not requested_tokens and not board_filter and self.settings.enabled and self.settings.use_builtin_board_universe
            if using_builtin_board_universe:
                seed_boards = [*seed_boards, *builtin_greenhouse_board_tokens()]
            seed_boards = list(dict.fromkeys(seed_boards))
            for board_token in seed_boards:
                discovery_method = "builtin" if using_builtin_board_universe and board_token in builtin_greenhouse_board_tokens() else "config"
                board_repo.upsert_board(BoardRegistry(source_adapter="greenhouse", board_token=board_token, active=True, discovery_method=discovery_method))
            if resume_run_id:
                run = run_repo.get_run(resume_run_id)
                if run is None:
                    raise ValueError(f"Run not found: {resume_run_id}")
                run_repo.reap_stale_leases()
            else:
                query = self.build_query(query_override)
                boards = list(board_repo.list_active_boards("greenhouse", limit=self.settings.max_boards_per_run))
                if requested_tokens:
                    boards_by_token = {board.board_token: board for board in boards}
                    for token in requested_tokens:
                        if token not in boards_by_token:
                            boards_by_token[token] = board_repo.upsert_board(BoardRegistry(source_adapter="greenhouse", board_token=token, active=True, discovery_method="manual"))
                    boards = [boards_by_token[token] for token in requested_tokens if token in boards_by_token]
                elif board_filter:
                    boards = [board for board in boards if board.board_token == board_filter]
                if max_boards is not None:
                    boards = boards[:max_boards]
                selected_tokens = [board.board_token for board in boards]
                run = run_repo.create_run(
                    run_type.value,
                    ApplicationMode.DRY_RUN,
                    checkpoint_state={
                        "query": query.model_dump(mode="json"),
                        "board_filter": board_filter,
                        "board_tokens": selected_tokens,
                        "max_boards": max_boards,
                        "using_builtin_board_universe": using_builtin_board_universe,
                    },
                )
                for board in boards:
                    run_repo.enqueue_task(
                        run.id,
                        TaskType.SYNC_BOARD.value,
                        {"board_token": board.board_token, "query": query.model_dump(mode="json")},
                        idempotency_key=f"greenhouse:sync:{board.board_token}",
                    )
                audit_repo.emit(
                    "run.started",
                    "run",
                    run.id,
                    run_id=run.id,
                    payload={"type": run_type.value, "boards": len(boards), "query": query.model_dump(mode="json")},
                )
        await self._process_run(run.id)
        return run.id

    async def benchmark(self, *, board_tokens: list[str] | None = None, max_boards: int | None = None) -> GreenhouseBenchmarkSummary:
        effective_max_boards = max_boards if max_boards is not None else min(5, self.settings.max_boards_per_run)
        run_id = await self.sync_boards(
            None,
            None,
            board_tokens=board_tokens,
            max_boards=effective_max_boards,
            run_type=RunType.BENCHMARK,
        )
        return self.load_benchmark_summary(run_id)

    def load_benchmark_summary(self, run_id: str) -> GreenhouseBenchmarkSummary:
        with self.runtime.session_scope() as session:
            run = RunRepository(session).get_run(run_id)
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            return self._benchmark_summary_from_run(run)

    def list_benchmarks(self, limit: int = 10) -> list[GreenhouseBenchmarkSummary]:
        with self.runtime.session_scope() as session:
            runs = RunRepository(session).list_runs_by_type(RunType.BENCHMARK.value, limit=limit)
            return [self._benchmark_summary_from_run(run) for run in runs]

    async def validate_board(self, board_token: str) -> dict[str, Any]:
        validation_status = "valid"
        error: str | None = None
        active = True
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                payload = self._normalize_board_metadata(await self.scale.validate_board(client, board_token), board_token)
        except Exception as exc:
            if not self._is_missing_board_error(exc):
                raise
            payload = {"name": board_token}
            validation_status = "invalid"
            error = str(exc)
            active = False
        with self.runtime.session_scope() as session:
            board = BoardRepository(session).mark_validation(
                "greenhouse",
                board_token,
                validation_status=validation_status,
                company_hint=payload.get("name") or board_token,
                board_url=f"https://boards.greenhouse.io/{board_token}",
                source_url=f"https://boards.greenhouse.io/{board_token}",
                source_domain="boards.greenhouse.io",
                error=error,
                active=active,
                notes={"metadata": payload},
            )
            return {"board_token": board.board_token, "validation_status": board.validation_status, "company_hint": board.company_hint, "error": error}

    async def _process_run(self, run_id: str) -> None:
        workers = max(1, self.settings.concurrency)

        async def worker(index: int) -> None:
            worker_name = f"greenhouse-{index}-{run_id[:8]}"
            while True:
                try:
                    async with self._claim_lock:
                        async with self._db_write_lock:
                            with self.runtime.session_scope() as session:
                                task = RunRepository(session).claim_task(worker_name, run_id=run_id, lease_seconds=600)
                                if task is None:
                                    return
                                task_id = task.id
                                task_type = task.task_type
                                payload = dict(task.payload)
                    try:
                        if task_type == TaskType.DISCOVER_BOARDS.value:
                            await self._handle_discover_boards(run_id, task_id, payload)
                        elif task_type == TaskType.SYNC_BOARD.value:
                            await self._handle_sync_board(run_id, task_id, payload)
                        elif task_type == TaskType.ENRICH_JOB.value:
                            await self._handle_enrich_job(run_id, task_id, payload)
                        else:
                            raise ValueError(f"Unsupported task type: {task_type}")
                    except Exception as exc:
                        try:
                            if task_type == TaskType.SYNC_BOARD.value and payload.get("board_token"):
                                await self._record_sync_failure(str(payload.get("board_token")), str(exc))
                            async with self._db_write_lock:
                                with self.runtime.session_scope() as session:
                                    task = RunRepository(session).finish_task(task_id, TaskStatus.FAILED_RETRYABLE, error=str(exc))
                                    AuditRepository(session).emit("task.failed", "task", task_id, run_id=run_id, task_id=task_id, payload={"error": str(exc), "status": task.status.value})
                        except Exception as inner_exc:
                            import logging
                            logging.getLogger(__name__).warning("Worker %s: failed to record task failure: %s (original: %s)", worker_name, inner_exc, exc)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).exception("Worker %s crashed: %s", worker_name, exc)
                    return

        async with anyio.create_task_group() as tg:
            for index in range(workers):
                tg.start_soon(worker, index)

        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            run = run_repo.get_run(run_id)
            tasks = list(run_repo.list_tasks(run_id))
            failed = [task for task in tasks if task.status in {TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED_TERMINAL, TaskStatus.RUNNING}]
            sync_tasks = [task for task in tasks if task.task_type == TaskType.SYNC_BOARD.value]
            successful_sync_tasks = [task for task in sync_tasks if task.status == TaskStatus.COMPLETED and not bool((task.checkpoint_state or {}).get("skipped"))]
            skipped_sync_tasks = [task for task in sync_tasks if bool((task.checkpoint_state or {}).get("skipped"))]
            duration_seconds = 0.0
            if run is not None and run.started_at is not None:
                started_at = run.started_at if run.started_at.tzinfo is not None else run.started_at.replace(tzinfo=timezone.utc)
                duration_seconds = max((utcnow() - started_at).total_seconds(), 0.0)
            jobs_seen = self._run_metrics.get("jobs_seen", 0)
            checkpoint = {
                "completed_tasks": len([task for task in tasks if task.status == TaskStatus.COMPLETED]),
                "failed_tasks": len(failed),
                "boards_attempted": len(sync_tasks),
                "boards_succeeded": len(successful_sync_tasks),
                "boards_skipped": len(skipped_sync_tasks),
                "failure_count": len(failed),
                "duration_seconds": round(duration_seconds, 3),
                "jobs_per_minute": round((jobs_seen / duration_seconds) * 60, 2) if duration_seconds > 0 else 0.0,
                **self._run_metrics,
                **self.scale.stats(),
            }
            run_repo.complete_run(run_id, RunStatus.FAILED if failed else RunStatus.COMPLETED, checkpoint_state=checkpoint)

    async def _heartbeat(self, task_id: str) -> None:
        for attempt in range(3):
            try:
                async with self._db_write_lock:
                    with self.runtime.session_scope() as session:
                        RunRepository(session).heartbeat(task_id, lease_seconds=600)
                return
            except Exception:
                if attempt == 2:
                    return  # heartbeat is best-effort; don't crash the worker
                await anyio.sleep(0.5 * (attempt + 1))

    async def _reserve_enrichment(self) -> bool:
        async with self._budget_lock:
            if self._remaining_enrichments <= 0:
                return False
            self._remaining_enrichments -= 1
            return True

    async def _reserve_jobs(self, count: int) -> int:
        async with self._budget_lock:
            allowed = max(0, min(count, self._remaining_jobs))
            self._remaining_jobs -= allowed
            return allowed

    def _reset_run_metrics(self) -> None:
        self._remaining_jobs = self.settings.max_jobs_per_run
        self._remaining_enrichments = self.settings.max_job_enrichment_tasks_per_run
        self._run_metrics = {
            "boards_processed": 0,
            "jobs_seen": 0,
            "enriched_jobs": 0,
            "inactive_jobs": 0,
            "backoff_boards": 0,
        }

    async def _add_metric(self, key: str, value: int = 1) -> None:
        async with self._metrics_lock:
            self._run_metrics[key] = self._run_metrics.get(key, 0) + value

    def _board_lock(self, board_token: str) -> anyio.Lock:
        if board_token not in self._board_locks:
            self._board_locks[board_token] = anyio.Lock()
        return self._board_locks[board_token]

    def _normalize_board_metadata(self, payload: Any, board_token: str) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    return item
        return {"name": board_token}

    def _normalize_jobs_payload(self, payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if isinstance(payload, dict):
            items = payload.get("jobs")
            if not isinstance(items, list):
                return payload, []
            return payload, [item for item in items if isinstance(item, dict)]
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            return {"jobs": items}, items
        return {"jobs": []}, []

    def _normalize_detail_payload(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return next((item for item in payload if isinstance(item, dict)), {})
        return {}

    def _employment_type(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("employment_type")
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _is_missing_board_error(exc: Exception) -> bool:
        return isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code == 404

    async def _finish_missing_board(self, run_id: str, task_id: str, board_token: str, error: str) -> None:
        async with self._db_write_lock:
            with self.runtime.session_scope() as session:
                board_repo = BoardRepository(session)
                state_repo = SourceStateRepository(session)
                run_repo = RunRepository(session)
                audit_repo = AuditRepository(session)
                live_count = board_repo.count_live_jobs_for_board("greenhouse", board_token)
                board = board_repo.mark_validation(
                    "greenhouse",
                    board_token,
                    validation_status="invalid",
                    company_hint=board_token,
                    board_url=f"https://boards.greenhouse.io/{board_token}",
                    source_url=f"https://boards.greenhouse.io/{board_token}",
                    source_domain="boards.greenhouse.io",
                    error=error,
                    active=False,
                )
                board.last_sync_at = utcnow()
                board.last_sync_status = "skipped_invalid"
                board.live_job_count = live_count
                board.last_error = error
                state = state_repo.get_board_sync_state("greenhouse", board_token)
                state.failure_count += 1
                state.last_failure_at = utcnow()
                state.last_validation_result = "invalid"
                state.backoff_until = None
                state_repo.save_board_sync_state(state)
                audit_repo.emit(
                    "board.invalid",
                    "board_registry",
                    board.id,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"board_token": board_token, "error": error},
                )
                run_repo.finish_task(
                    task_id,
                    TaskStatus.COMPLETED,
                    checkpoint_state={"board_token": board_token, "skipped": True, "reason": "board_not_found"},
                )

    async def _handle_discover_boards(self, run_id: str, task_id: str, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            candidates = await self.scale.discover_boards(
                client,
                seed_urls=list(payload.get("seed_urls") or []),
                seed_domains=list(payload.get("seed_domains") or []),
                max_boards=self.settings.max_boards_per_run,
            )
            validated: list[tuple[dict[str, Any], Any]] = []
            for candidate in candidates:
                try:
                    metadata = self._normalize_board_metadata(await self.scale.validate_board(client, candidate.board_token), candidate.board_token)
                    validated.append((metadata, candidate))
                except Exception:
                    continue
        async with self._db_write_lock:
            with self.runtime.session_scope() as session:
                board_repo = BoardRepository(session)
                run_repo = RunRepository(session)
                audit_repo = AuditRepository(session)
                for metadata, candidate in validated:
                    board = board_repo.mark_validation(
                        "greenhouse",
                        candidate.board_token,
                        validation_status="valid",
                        company_hint=metadata.get("name") or candidate.board_token,
                        board_url=f"https://boards.greenhouse.io/{candidate.board_token}",
                        source_url=candidate.source_url,
                        source_domain=candidate.source_domain,
                        notes={"metadata": metadata},
                    )
                    board_repo.record_evidence(
                        BoardDiscoveryEvidence(
                            board_token=board.board_token,
                            source_adapter="greenhouse",
                            source_url=candidate.source_url,
                        discovery_method=candidate.discovery_method,
                        payload={"source_domain": candidate.source_domain},
                    )
                )
                    audit_repo.emit("board.discovered", "board_registry", board.id, run_id=run_id, task_id=task_id, payload={"board_token": board.board_token})
                run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"boards": len(validated), **self.scale.stats()})

    async def _handle_sync_board(self, run_id: str, task_id: str, payload: dict[str, Any]) -> None:
        board_token = str(payload["board_token"])
        async with self._board_lock(board_token):
            async with self._db_write_lock:
                with self.runtime.session_scope() as session:
                    state = SourceStateRepository(session).get_board_sync_state("greenhouse", board_token)
                    if state.backoff_until and state.backoff_until > utcnow():
                        RunRepository(session).finish_task(task_id, TaskStatus.FAILED_TERMINAL, checkpoint_state={"backoff": True})
                        return
                    facts = [self._fact_model(record) for record in ProfileRepository(session).list_facts()]
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    metadata = self._normalize_board_metadata(await self.scale.validate_board(client, board_token), board_token)
                    jobs_payload_raw = await self.scale.fetch_board_jobs(client, board_token)
            except Exception as exc:
                if self._is_missing_board_error(exc):
                    await self._finish_missing_board(run_id, task_id, board_token, str(exc))
                    return
                raise
            await self._heartbeat(task_id)
            jobs_payload, items = self._normalize_jobs_payload(jobs_payload_raw)
            allowed = await self._reserve_jobs(len(items))
            items = items[:allowed]
            seen_ids = {str(item.get("id")) for item in items}
            query = DiscoveryQuery.model_validate(payload.get("query") or self.build_query().model_dump(mode="json"))
            changed_jobs: list[tuple[str, str]] = []
            for item in items:
                await self._store_greenhouse_job(run_id, task_id, board_token, item, metadata, facts, query, changed_jobs)
            async with self._db_write_lock:
                with self.runtime.session_scope() as session:
                    board_repo = BoardRepository(session)
                    inactive = board_repo.mark_missing_jobs_inactive("greenhouse", board_token, seen_ids, self.settings.stale_job_threshold)
                    live_count = board_repo.count_live_jobs_for_board("greenhouse", board_token)
                    board_repo.mark_validation("greenhouse", board_token, validation_status="valid", company_hint=metadata.get("name") or board_token, board_url=f"https://boards.greenhouse.io/{board_token}", source_url=f"https://boards.greenhouse.io/{board_token}", source_domain="boards.greenhouse.io")
                    board_repo.mark_sync_result("greenhouse", board_token, status="success", live_job_count=live_count)
                    SourceStateRepository(session).save_board_sync_state(
                        BoardSyncState(board_token=board_token, source_adapter="greenhouse", payload_hash=hash_content(jobs_payload), job_count=len(items), last_validation_result="valid", last_success_at=utcnow())
                    )
                    run_repo = RunRepository(session)
                    audit_repo = AuditRepository(session)
                    for job_id, source_job_id in changed_jobs:
                        run_repo.enqueue_task(run_id, TaskType.ENRICH_JOB.value, {"job_id": job_id, "board_token": board_token, "source_job_id": source_job_id}, idempotency_key=f"greenhouse:enrich:{job_id}")
                    audit_repo.emit("board.synced", "board_registry", board_token, run_id=run_id, task_id=task_id, payload={"jobs": len(items), "inactive_jobs": len(inactive), "enriched": len(changed_jobs), **self.scale.stats()})
                    run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"jobs": len(items), "inactive_jobs": len(inactive), **self.scale.stats()})
            await self._add_metric("boards_processed")
            await self._add_metric("jobs_seen", len(items))
            await self._add_metric("inactive_jobs", len(inactive))

    async def _store_greenhouse_job(self, run_id: str, task_id: str, board_token: str, item: dict[str, Any], metadata: dict[str, Any], facts, query: DiscoveryQuery, changed_jobs: list[tuple[str, str]]) -> None:
        description = item.get("content") or ""
        location_text = self._greenhouse_location_text(item)
        posting = build_normalized_job(
            company_name=metadata.get("name") or board_token,
            title=item.get("title", "Untitled role"),
            source="greenhouse",
            source_kind="greenhouse",
            source_job_id=str(item.get("id")),
            posting_url=item.get("absolute_url"),
            apply_url=item.get("absolute_url"),
            location_raw=location_text,
            employment_type=self._employment_type(item),
            compensation=item.get("pay_input_ranges"),
            description=description,
            posted_at=extract_posted_at(item),
            source_updated_at=extract_source_updated_at(item),
            notes={"board": board_token, "list_payload_hash": hash_content(item), "metadata": metadata},
        )
        async with self._db_write_lock:
            with self.runtime.session_scope() as session:
                job_repo = JobRepository(session)
                audit_repo = AuditRepository(session)
                existing = session.scalar(select(JobPosting).where(JobPosting.source_adapter == "greenhouse", JobPosting.source_job_id == posting.source_job_id))
                previous_hash = (existing.notes or {}).get("list_payload_hash") if existing else None
                qualification = qualification_for_job(posting, query, facts)
                posting.lifecycle_status = qualification.decision
                record = job_repo.upsert_job(posting, raw_payload=item)
                record.board_token = board_token
                job_repo.save_qualification(record.id, qualification)
                if qualification.decision != JobLifecycleStatus.SCREENED_OUT and job_repo.duplicate_exists(record.duplicate_cluster_key, exclude_job_id=record.id):
                    record.lifecycle_status = JobLifecycleStatus.DUPLICATE_BLOCKED
                if previous_hash != posting.notes.get("list_payload_hash") and await self._reserve_enrichment():
                    changed_jobs.append((record.id, record.source_job_id))
                audit_repo.emit("job.discovered", "job_posting", record.id, run_id=run_id, task_id=task_id, payload={"board": board_token, "status": record.lifecycle_status.value})

    async def _handle_enrich_job(self, run_id: str, task_id: str, payload: dict[str, Any]) -> None:
        board_token = str(payload["board_token"])
        source_job_id = str(payload["source_job_id"])
        async with self._db_write_lock:
            with self.runtime.session_scope() as session:
                job = JobRepository(session).get_job(str(payload["job_id"]))
                if job is None:
                    raise ValueError(f"Job not found: {payload['job_id']}")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            detail = self._normalize_detail_payload(await self.scale.fetch_job_detail(client, board_token, source_job_id))
        async with self._db_write_lock:
            with self.runtime.session_scope() as session:
                job_repo = JobRepository(session)
                run_repo = RunRepository(session)
                audit_repo = AuditRepository(session)
                job = job_repo.get_job(str(payload["job_id"]))
                if job is None:
                    raise ValueError(f"Job not found: {payload['job_id']}")
                enriched = build_normalized_job(
                    company_name=job.company.display_name,
                    title=detail.get("title") or job.title,
                    source="greenhouse",
                    source_kind=job.source_kind,
                    source_job_id=job.source_job_id,
                    posting_url=job.posting_url,
                    apply_url=job.apply_url,
                    location_raw=self._greenhouse_location_text(detail) or job.location_raw,
                    employment_type=self._employment_type(detail) or job.employment_type,
                    compensation=detail.get("pay_input_ranges") or job.compensation,
                    description=detail.get("content") or job.description,
                    posted_at=extract_posted_at(detail) or job.posted_at,
                    source_updated_at=extract_source_updated_at(detail) or job.source_updated_at,
                    company_size_bucket=job.company.company_size_bucket,
                    company_employee_count_min=job.company.employee_count_min,
                    company_employee_count_max=job.company.employee_count_max,
                    notes={
                        **(job.notes or {}),
                        "detail_payload_hash": hash_content(detail),
                        "question_count": len(detail.get("questions") or []),
                        "location_question_count": len(detail.get("location_questions") or []),
                    },
                )
                record = job_repo.upsert_job(enriched, raw_payload=detail)
                record.board_token = board_token
                audit_repo.emit("job.enriched", "job_posting", record.id, run_id=run_id, task_id=task_id, payload={"board": board_token, **self.scale.stats()})
                run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"job_id": record.id, **self.scale.stats()})
        await self._add_metric("enriched_jobs")

    def _greenhouse_location_text(self, payload: dict[str, Any] | Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        location = payload.get("location")
        if isinstance(location, dict):
            name = str(location.get("name") or "").strip()
            if name:
                return name
        elif isinstance(location, str) and location.strip():
            return location.strip()
        offices = [str(office.get("name") or "").strip() for office in payload.get("offices", []) if isinstance(office, dict) and str(office.get("name") or "").strip()]
        if offices:
            return " | ".join(offices)
        return None

    def _fact_model(self, record):
        from findmyjob.core.types import ProfileFact

        return ProfileFact(
            fact_id=record.fact_id,
            kind=record.kind,
            payload=record.payload,
            sensitivity=record.sensitivity,
            allowed_for_generation=record.allowed_for_generation,
            disallowed=record.disallowed,
            provenance=record.provenance,
            confirmed=record.confirmed,
        )


    async def _record_sync_failure(self, board_token: str, error: str) -> None:
        self._run_metrics["backoff_boards"] = self._run_metrics.get("backoff_boards", 0) + 1
        async with self._db_write_lock:
            with self.runtime.session_scope() as session:
                board_repo = BoardRepository(session)
                state_repo = SourceStateRepository(session)
                board_repo.mark_validation("greenhouse", board_token, validation_status="invalid", error=error)
                board_repo.mark_sync_result("greenhouse", board_token, status="failed", live_job_count=board_repo.count_live_jobs_for_board("greenhouse", board_token), error=error)
                state = state_repo.get_board_sync_state("greenhouse", board_token)
                state.failure_count += 1
                state.last_failure_at = utcnow()
                state.last_validation_result = "invalid"
                state.backoff_until = utcnow() + timedelta(minutes=min(60, max(5, state.failure_count * 5)))
                state_repo.save_board_sync_state(state)








    def _benchmark_summary_from_run(self, run) -> GreenhouseBenchmarkSummary:
        checkpoint = dict(run.checkpoint_state or {})
        return GreenhouseBenchmarkSummary(
            run_id=run.id,
            status=run.status.value,
            board_tokens=list(checkpoint.get("board_tokens") or []),
            boards_attempted=int(checkpoint.get("boards_attempted") or 0),
            boards_succeeded=int(checkpoint.get("boards_succeeded") or 0),
            jobs_seen=int(checkpoint.get("jobs_seen") or 0),
            jobs_enriched=int(checkpoint.get("enriched_jobs") or 0),
            inactive_jobs=int(checkpoint.get("inactive_jobs") or 0),
            request_count=int(checkpoint.get("request_count") or 0),
            rate_limited_count=int(checkpoint.get("rate_limited_count") or 0),
            failure_count=int(checkpoint.get("failure_count") or 0),
            duration_seconds=float(checkpoint.get("duration_seconds") or 0.0),
            jobs_per_minute=float(checkpoint.get("jobs_per_minute") or 0.0),
            started_at=run.started_at,
            completed_at=run.completed_at,
        )

