from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("findmyjob.orchestrator")

from findmyjob.apply.browser import analyze_dom_snapshot, classify_submission_failure
from findmyjob.apply.service import ApplicationService
from findmyjob.core.enums import (
    ApplicationMode,
    ArtifactKind,
    JobLifecycleStatus,
    PolicyMode,
    QuestionType,
    SubmitEvidenceMode,
    ReviewStatus,
    RunStatus,
    RunType,
    TaskStatus,
    TaskType,
    VerificationStatus,
)
from findmyjob.core.logging import redact_data
from findmyjob.core.policies import resolve_source_policy
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import ApplicationQuestion, ArtifactBinding, ArtifactDraft, GroundedAnswer, JobSearchQuery, ProfileFact, ReviewPacket, SmokeTestResult, SubmissionPlan
from findmyjob.db.models import ApplicationAnswerRecord, ApplicationQuestionRecord, JobPosting
from findmyjob.db.repositories import ApplicationRepository, AuditRepository, JobRepository, ProfileRepository, RunRepository, hash_content
from findmyjob.qualification.rules import qualification_for_job
from findmyjob.sources.adapters.ashby import AshbyAdapter
from findmyjob.sources.adapters.greenhouse import GreenhouseAdapter
from findmyjob.sources.adapters.lever import LeverAdapter
from findmyjob.sources.base import SourceAdapter
from findmyjob.sources.classification import classify_job
from findmyjob.sources.contracts import DiscoveryQuery
from findmyjob.sources.greenhouse_scale import GreenhouseScaleClient
from findmyjob.sources.normalizer import build_normalized_job


class Orchestrator:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    def build_query(self, query_override: JobSearchQuery | None = None) -> DiscoveryQuery:
        if query_override is not None:
            return query_override.to_discovery_query()
        return JobSearchQuery.from_search_settings(self.runtime.config.search).to_discovery_query()

    def source_adapters(self, query_override: JobSearchQuery | None = None) -> dict[str, SourceAdapter]:
        adapters: dict[str, SourceAdapter] = {}
        source_filter = query_override.source_adapter if query_override else None
        board_filter = query_override.board_token if query_override else None
        for name, settings in self.runtime.config.sources.items():
            if source_filter and name != source_filter:
                continue
            if not settings.enabled:
                continue
            boards = [board for board in settings.boards if not board_filter or board == board_filter]
            if name == "greenhouse":
                adapter_boards = boards or ([board_filter] if board_filter else list(settings.boards))
                adapters[name] = GreenhouseAdapter(adapter_boards)
                continue
            if not boards:
                continue
            if name == "lever":
                adapters[name] = LeverAdapter(boards)
            elif name == "ashby":
                adapters[name] = AshbyAdapter(boards)
        return adapters

    async def run_discovery(
        self,
        mode: ApplicationMode = ApplicationMode.DRY_RUN,
        resume_run_id: str | None = None,
        query_override: JobSearchQuery | None = None,
    ) -> str:
        query = self.build_query(query_override)
        adapters = self.source_adapters(query_override)
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            if resume_run_id:
                run = run_repo.get_run(resume_run_id)
                if run is None:
                    raise ValueError(f"Run not found: {resume_run_id}")
                run_repo.reap_stale_leases()
            else:
                run = run_repo.create_run(RunType.DISCOVER.value, mode, checkpoint_state={"query": query.model_dump(mode="json")})
                for adapter_name, adapter in adapters.items():
                    for board in adapter.boards:
                        run_repo.enqueue_task(
                            run.id,
                            TaskType.DISCOVER_BOARD.value,
                            {"adapter_name": adapter_name, "board": board, "query": query.model_dump(mode="json")},
                            idempotency_key=f"discover:{adapter_name}:{board}",
                        )
                audit_repo.emit("run.started", "run", run.id, run_id=run.id, payload={"mode": mode.value, "type": RunType.DISCOVER.value})
        await self._process_run(run.id)
        return run.id

    async def run_prepare(self, mode: ApplicationMode = ApplicationMode.DRY_RUN, resume_run_id: str | None = None) -> str:
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            job_repo = JobRepository(session)
            if resume_run_id:
                run = run_repo.get_run(resume_run_id)
                if run is None:
                    raise ValueError(f"Run not found: {resume_run_id}")
                run_repo.reap_stale_leases()
            else:
                run = run_repo.create_run(RunType.PREPARE.value, mode, checkpoint_state={})
                for job in job_repo.list_jobs_for_preparation():
                    run_repo.enqueue_task(
                        run.id,
                        TaskType.PREPARE_APPLICATION.value,
                        {"job_id": job.id},
                        idempotency_key=f"prepare:{job.id}",
                    )
                audit_repo.emit("run.started", "run", run.id, run_id=run.id, payload={"mode": mode.value, "type": RunType.PREPARE.value})
        await self._process_run(run.id)
        return run.id

    async def run_apply(self, mode: ApplicationMode = ApplicationMode.AUTO_SUBMIT, resume_run_id: str | None = None) -> str:
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            app_repo = ApplicationRepository(session)
            if resume_run_id:
                run = run_repo.get_run(resume_run_id)
                if run is None:
                    raise ValueError(f"Run not found: {resume_run_id}")
                run_repo.reap_stale_leases()
            else:
                run = run_repo.create_run(RunType.APPLY.value, mode, checkpoint_state={})
                for application in app_repo.list_approved_for_submit():
                    run_repo.enqueue_task(
                        run.id,
                        TaskType.SUBMIT_APPLICATION.value,
                        {"application_id": application.id},
                        idempotency_key=f"submit:{application.id}",
                    )
                audit_repo.emit("run.started", "run", run.id, run_id=run.id, payload={"mode": mode.value, "type": RunType.APPLY.value})
        await self._process_run(run.id)
        return run.id

    async def run_prepare_for_job(self, job_id: str, mode: ApplicationMode = ApplicationMode.DRY_RUN) -> str:
        with self.runtime.session_scope() as session:
            job = JobRepository(session).get_job(job_id)
            if job is None:
                raise ValueError(f"Job not found: {job_id}")
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            run = run_repo.create_run(RunType.PREPARE.value, mode, checkpoint_state={"job_id": job_id})
            run_repo.enqueue_task(
                run.id,
                TaskType.PREPARE_APPLICATION.value,
                {"job_id": job_id},
                idempotency_key=f"prepare:{job_id}",
            )
            audit_repo.emit("run.started", "run", run.id, run_id=run.id, payload={"mode": mode.value, "type": RunType.PREPARE.value, "job_id": job_id})
        await self._process_run(run.id)
        return run.id

    async def run_apply_for_application(self, application_id: str, mode: ApplicationMode = ApplicationMode.AUTO_SUBMIT) -> str:
        with self.runtime.session_scope() as session:
            application = ApplicationRepository(session).get_application(application_id)
            if application is None:
                raise ValueError(f"Application not found: {application_id}")
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            run = run_repo.create_run(RunType.APPLY.value, mode, checkpoint_state={"application_id": application_id})
            run_repo.enqueue_task(
                run.id,
                TaskType.SUBMIT_APPLICATION.value,
                {"application_id": application_id},
                idempotency_key=f"submit:{application_id}",
            )
            audit_repo.emit("run.started", "run", run.id, run_id=run.id, payload={"mode": mode.value, "type": RunType.APPLY.value, "application_id": application_id})
        await self._process_run(run.id)
        return run.id

    async def resume_run(self, run_id: str) -> str:
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            run = run_repo.get_run(run_id)
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            run_repo.reap_stale_leases()
        await self._process_run(run_id)
        return run_id

    async def review_action(self, application_id: str, action: ReviewStatus, reason: str | None = None) -> str:
        with self.runtime.session_scope() as session:
            app_repo = ApplicationRepository(session)
            audit_repo = AuditRepository(session)
            application = app_repo.set_review_status(application_id, action, reason=reason)
            audit_repo.emit("review.action", "application", application.id, payload={"action": action.value, "reason": reason})
            return application.status.value

    async def answer_questions(self, questions: list[str]) -> list[GroundedAnswer]:
        with self.runtime.session_scope() as session:
            facts = [self._fact_model(record) for record in ProfileRepository(session).list_facts()]
        return [await self.runtime.grounding.answer_question(question, facts) for question in questions]

    def probe_sources(self) -> list[dict[str, Any]]:
        probes = []
        for name, adapter in self.source_adapters().items():
            settings = self.runtime.config.sources.get(name)
            probes.append(
                {
                    "name": name,
                    **adapter.capabilities().model_dump(mode="json"),
                    "boards": adapter.boards,
                    "submit_enabled": settings.submit_enabled if settings else False,
                    "live_smoke_urls": settings.live_smoke_urls if settings else [],
                }
            )
        return probes

    async def live_check_sources(self, query_override: JobSearchQuery | None = None) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        query = self.build_query(query_override)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for name, adapter in self.source_adapters(query_override).items():
                try:
                    discovered = await adapter.discover(client, query)
                    checks.append({"source": name, "ok": True, "count": len(discovered)})
                except Exception as exc:
                    checks.append({"source": name, "ok": False, "error": str(exc)})
        return checks

    async def run_greenhouse_smoke_test(self, board_token: str, source_job_id: str, *, confirm: bool = False) -> SmokeTestResult:
        result = SmokeTestResult(
            board_token=board_token,
            source_job_id=str(source_job_id),
            submit_confirmed=confirm,
        )
        try:
            settings = self.runtime.config.sources.get("greenhouse")
            if settings is None or not settings.enabled:
                raise RuntimeError("Greenhouse source is not enabled in workspace config.")
            allowed_urls = [item.rstrip("/") for item in settings.live_smoke_urls if item.strip()]
            if not allowed_urls:
                raise RuntimeError("Smoke test refused because sources.greenhouse.live_smoke_urls is empty. Add an explicit allowlisted posting URL first.")

            scale = GreenhouseScaleClient(
                request_timeout_seconds=settings.request_timeout_seconds,
                requests_per_second=settings.per_host_request_rate,
                crawl_depth=settings.crawl_depth,
            )
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=True) as client:
                metadata = await scale.validate_board(client, board_token)
                jobs_payload = await scale.fetch_board_jobs(client, board_token)
                detail = await scale.fetch_job_detail(client, board_token, str(source_job_id))
            if isinstance(metadata, list):
                metadata = next((row for row in metadata if isinstance(row, dict)), {"name": board_token})
            elif not isinstance(metadata, dict):
                metadata = {"name": board_token}
            if isinstance(jobs_payload, dict):
                jobs = [row for row in jobs_payload.get("jobs", []) if isinstance(row, dict)]
            elif isinstance(jobs_payload, list):
                jobs = [row for row in jobs_payload if isinstance(row, dict)]
            else:
                jobs = []
            if isinstance(detail, list):
                detail = next((row for row in detail if isinstance(row, dict)), {})
            elif not isinstance(detail, dict):
                detail = {}
            item = next((row for row in jobs if str(row.get("id")) == str(source_job_id)), None)
            if item is None:
                raise RuntimeError(f"Job {source_job_id} was not found on board {board_token}.")
            apply_url = str(item.get("absolute_url") or "").rstrip("/")
            if apply_url not in allowed_urls:
                raise RuntimeError("Smoke test refused because the job apply URL is not explicitly allowlisted in sources.greenhouse.live_smoke_urls.")

            result.apply_url = apply_url
            office_names = ", ".join(
                office.get("name", "")
                for office in item.get("offices", [])
                if isinstance(office, dict) and str(office.get("name") or "").strip()
            ) or None
            compensation = detail.get("pay_input_ranges") or item.get("pay_input_ranges")
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            posting = build_normalized_job(
                company_name=metadata.get("name") or board_token,
                title=item.get("title", "Untitled role"),
                source="greenhouse",
                source_kind="greenhouse",
                source_job_id=str(item.get("id")),
                posting_url=apply_url,
                apply_url=apply_url,
                location_raw=office_names,
                employment_type=item_metadata.get("employment_type"),
                compensation=compensation,
                description=item.get("content") or detail.get("content") or "",
                notes={
                    "board": board_token,
                    **self._source_runtime_notes("greenhouse"),
                    "smoke_test": True,
                    "question_count": len(detail.get("questions") or []),
                },
            )
            with self.runtime.session_scope() as session:
                facts = [self._fact_model(record) for record in ProfileRepository(session).list_facts()]
                qualification = qualification_for_job(posting, self.build_query(), facts)
                posting.lifecycle_status = qualification.decision
                job_record = JobRepository(session).upsert_job(posting, raw_payload=item)
                job_record.board_token = board_token
                job_record.notes = posting.notes
                JobRepository(session).save_qualification(job_record.id, qualification)
                job_id_local = job_record.id

            prepare_run_id = await self.run_prepare_for_job(job_id_local, ApplicationMode.DRY_RUN)
            plan = await self.inspect_submission_plan(job_id_local)
            with self.runtime.session_scope() as session:
                app_repo = ApplicationRepository(session)
                application = app_repo.get_application_for_job(job_id_local)
                if application is None:
                    raise RuntimeError("Smoke test preparation did not create an application record.")
                packet = next((artifact for artifact in app_repo.list_artifacts(application_id=application.id) if artifact.kind.value == "review_packet"), None)
                result.job_posting_id = job_id_local
                result.application_id = application.id
                result.references = {
                    "prepare_run_id": prepare_run_id,
                    "review_packet": packet.path if packet else None,
                    "application_status": application.status.value,
                    "plan_missing_required_fields": list(plan.missing_required_fields if plan is not None else []),
                    "plan_notes": list(plan.notes if plan is not None else []),
                }
                result.notes = list(plan.notes if plan is not None else [])
                submit_ready = application.status == JobLifecycleStatus.READY_FOR_REVIEW

            if not confirm:
                if submit_ready:
                    result.status = "pass"
                else:
                    result.status = "fail"
                    result.failure_reason = f"prepared application is not submit-ready ({result.references['application_status']})"
                return result

            if not submit_ready:
                raise RuntimeError(f"Smoke test submit requires a prepared application. Current status: {result.references['application_status']}")

            await self.review_action(result.application_id or "", ReviewStatus.APPROVED, "Controlled Greenhouse smoke test")
            submit_run_id = await self.run_apply_for_application(result.application_id or "", ApplicationMode.AUTO_SUBMIT)
            inspection = await self.inspect_submission_result(result.application_id or "")
            result.references.update(
                {
                    "submit_run_id": submit_run_id,
                    "receipt_path": inspection.get("receipt_path") if inspection else None,
                    "trace_path": inspection.get("trace_path") if inspection else None,
                    "pre_submit_snapshot_path": inspection.get("pre_submit_snapshot_path") if inspection else None,
                    "final_snapshot_path": inspection.get("final_snapshot_path") if inspection else None,
                    "dom_snapshot_path": inspection.get("dom_snapshot_path") if inspection else None,
                    "post_submit_dom_snapshot_path": inspection.get("post_submit_dom_snapshot_path") if inspection else None,
                    "final_url": inspection.get("final_url") if inspection else None,
                }
            )
            if inspection and inspection.get("submission_status") == JobLifecycleStatus.SUBMITTED.value:
                result.status = "pass"
            elif inspection and inspection.get("submission_status") == JobLifecycleStatus.SUBMISSION_UNCERTAIN.value:
                result.status = "warning"
                result.failure_reason = inspection.get("failure_reason") or "submission_uncertain"
            else:
                result.status = "fail"
                result.failure_reason = inspection.get("failure_reason") if inspection else "submission_failed"
        except Exception as exc:
            result.status = "fail"
            result.failure_reason = str(exc)
        finally:
            self._record_smoke_test_result(result)
        return result

    def _record_smoke_test_result(self, result: SmokeTestResult) -> None:
        payload = result.model_dump(mode="json")
        with self.runtime.session_scope() as session:
            AuditRepository(session).emit(
                "greenhouse.smoke_test.recorded",
                "greenhouse_smoke",
                entity_id=result.application_id or result.source_job_id,
                payload=payload,
            )
    async def inspect_submission_plan(self, job_id: str) -> SubmissionPlan | None:
        with self.runtime.session_scope() as session:
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)
            job = job_repo.get_job(job_id)
            if job is None:
                raise ValueError(f"Job not found: {job_id}")
            application = app_repo.get_application_for_job(job_id)
            if application is None:
                return None
            adapter = self.source_adapters().get(job.source_adapter)
            if adapter is None:
                return None
            question_answers = app_repo.list_answers_for_application(application.id)
            artifacts = app_repo.list_artifacts(application_id=application.id)
            artifact_paths = {artifact.kind.value: artifact.path for artifact in artifacts}
            return adapter.bind_answers(self._job_model(job), question_answers, artifact_paths)

    async def inspect_submission_result(self, application_id: str) -> dict[str, Any] | None:
        with self.runtime.session_scope() as session:
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)
            application = app_repo.get_application(application_id)
            if application is None:
                raise ValueError(f"Application not found: {application_id}")
            job = job_repo.get_job(application.job_posting_id)
            if job is None:
                raise ValueError(f"Job not found for application: {application_id}")
            latest_attempt = app_repo.latest_submit_attempt(application.id)
            artifacts = app_repo.list_artifacts(application_id=application.id)
            payload = latest_attempt.payload if latest_attempt is not None else {}
            evidence = payload.get("evidence") or {}
            field_audit = list(evidence.get("field_audit") or [])
            return {
                "application_id": application.id,
                "job_id": job.id,
                "job_title": job.title,
                "company": job.company.display_name,
                "submission_status": application.status.value,
                "review_status": application.review_status.value,
                "failure_reason": evidence.get("failure_reason"),
                "confirmation_strategy": evidence.get("confirmation_strategy"),
                "confirmation_text": evidence.get("confirmation_text"),
                "final_url": evidence.get("final_url"),
                "receipt_path": next((artifact.path for artifact in artifacts if artifact.kind == ArtifactKind.SUBMISSION_RECEIPT), None),
                "trace_path": next((artifact.path for artifact in artifacts if artifact.kind == ArtifactKind.SUBMISSION_TRACE), None),
                "pre_submit_snapshot_path": evidence.get("pre_submit_snapshot_path"),
                "final_snapshot_path": evidence.get("final_snapshot_path"),
                "dom_snapshot_path": evidence.get("dom_snapshot_path"),
                "post_submit_dom_snapshot_path": evidence.get("post_submit_dom_snapshot_path"),
                "validation_errors": list(evidence.get("visible_validation_errors") or []),
                "matched_confirmation_markers": list(evidence.get("matched_confirmation_markers") or []),
                "missing_required_controls": list(evidence.get("missing_required_controls") or []),
                "submit_button_present": evidence.get("submit_button_present"),
                "submit_button_enabled": evidence.get("submit_button_enabled"),
                "field_audit_summary": [
                    {
                        "field": row.get("field"),
                        "prompt": row.get("prompt"),
                        "status": row.get("status"),
                        "failure_reason": row.get("failure_reason"),
                        "error_text": row.get("error_text"),
                        "value_summary": row.get("value_summary"),
                    }
                    for row in field_audit
                ],
                "field_audit": field_audit,
                "attempt_recorded_at": str(latest_attempt.created_at) if latest_attempt is not None else None,
                "attempt_payload": payload,
            }

    async def _process_run(self, run_id: str) -> None:
        worker_name = f"orchestrator-{run_id[:8]}"
        while True:
            with self.runtime.session_scope() as session:
                run_repo = RunRepository(session)
                task = run_repo.claim_task(worker_name, run_id=run_id, lease_seconds=600)
                if task is None:
                    break
                task_id = task.id
                task_type = task.task_type
                payload = dict(task.payload)

            log.info("  [orchestrator] Processing task: type=%s, task_id=%s", task_type, task_id[:12])
            try:
                if task_type == TaskType.DISCOVER_BOARD.value:
                    await self._process_discover_task(run_id, task_id, payload)
                elif task_type == TaskType.PREPARE_APPLICATION.value:
                    await self._process_prepare_task(run_id, task_id, payload)
                elif task_type == TaskType.SUBMIT_APPLICATION.value:
                    await self._process_submit_task(run_id, task_id, payload)
                else:
                    raise ValueError(f"Unsupported task type: {task_type}")
                log.info("  [orchestrator] Task %s completed successfully", task_id[:12])
            except Exception as exc:
                log.error("  [orchestrator] Task %s FAILED: %s", task_id[:12], exc, exc_info=True)
                with self.runtime.session_scope() as session:
                    run_repo = RunRepository(session)
                    audit_repo = AuditRepository(session)
                    task = run_repo.finish_task(task_id, TaskStatus.FAILED_RETRYABLE, error=str(exc))
                    audit_repo.emit(
                        "task.failed",
                        "task",
                        task_id,
                        run_id=run_id,
                        task_id=task_id,
                        payload={"error": str(exc), "status": task.status.value, "attempt_count": task.attempt_count},
                    )

        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            tasks = list(run_repo.list_tasks(run_id))
            failed = [task for task in tasks if task.status in {TaskStatus.FAILED_TERMINAL, TaskStatus.RUNNING}]
            checkpoint = {
                "completed_tasks": len([task for task in tasks if task.status == TaskStatus.COMPLETED]),
                "failed_tasks": len(failed),
            }
            final_status = RunStatus.FAILED if failed else RunStatus.COMPLETED
            run_repo.complete_run(run_id, final_status, checkpoint_state=checkpoint)

    async def _process_discover_task(self, run_id: str, task_id: str, payload: dict[str, Any]) -> None:
        adapter_name = str(payload["adapter_name"])
        board = str(payload["board"])
        query = DiscoveryQuery.model_validate(payload.get("query") or {})
        adapter = self._adapter_for_single_board(adapter_name, board)

        with self.runtime.session_scope() as session:
            facts = [self._fact_model(record) for record in ProfileRepository(session).list_facts()]

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            discovered = await adapter.discover(client, query)

        with self.runtime.session_scope() as session:
            job_repo = JobRepository(session)
            run_repo = RunRepository(session)
            audit_repo = AuditRepository(session)
            saved = 0
            for posting, raw_payload in discovered:
                posting.notes = {**posting.notes, **self._source_runtime_notes(adapter_name), "board": board}
                qualification = qualification_for_job(posting, query, facts)
                posting.lifecycle_status = qualification.decision
                record = job_repo.upsert_job(posting, raw_payload=raw_payload)
                job_repo.save_qualification(record.id, qualification)
                if qualification.decision != JobLifecycleStatus.SCREENED_OUT and job_repo.duplicate_exists(record.duplicate_cluster_key, exclude_job_id=record.id):
                    record.lifecycle_status = JobLifecycleStatus.DUPLICATE_BLOCKED
                job_classification = classify_job(
                    source_kind=posting.source_kind,
                    apply_url=str(posting.apply_url or ""),
                    posting_url=str(posting.posting_url),
                    source_adapter=adapter_name,
                )
                audit_repo.emit(
                    "job.discovered",
                    "job_posting",
                    record.id,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "source": adapter_name,
                        "board": board,
                        "status": record.lifecycle_status.value,
                        "board_family": job_classification.board_family.value,
                        "automation_tier": job_classification.automation_tier.value,
                    },
                )
                saved += 1
            run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"count": saved, "board": board})
    async def _process_prepare_task(self, run_id: str, task_id: str, payload: dict[str, Any]) -> None:
        job_id = str(payload["job_id"])
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)
            profile_repo = ProfileRepository(session)
            run = run_repo.get_run(run_id)
            job = job_repo.get_job(job_id)
            if run is None or job is None:
                raise ValueError(f"Preparation target missing for job {job_id}")
            adapter = self.source_adapters().get(job.source_adapter)
            if adapter is None:
                raise ValueError(f"No adapter configured for {job.source_adapter}")
            application = app_repo.ensure_application(job.id, run.mode)
            application.status = JobLifecycleStatus.PREPARING
            job.lifecycle_status = JobLifecycleStatus.PREPARING
            job_model = self._job_model(job)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            extraction = await adapter.load_application_contract(client, job_model)

        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)
            profile_repo = ProfileRepository(session)
            audit_repo = AuditRepository(session)
            job = job_repo.get_job(job_id)
            if job is None:
                raise ValueError(f"Job not found: {job_id}")
            application = app_repo.get_application_for_job(job.id)
            if application is None:
                raise ValueError(f"Application not found for job {job.id}")
            app_repo.clear_questions(application.id)
            stored_questions: list[tuple[ApplicationQuestionRecord, ApplicationQuestion]] = []
            for question in extraction.questions:
                stored_questions.append((app_repo.store_question(application.id, question), question))
            facts = [self._fact_model(record) for record in profile_repo.list_facts()]
            job_model = self._job_model(job)

            blocking_artifact_failures: list[str] = []
            try:
                autonomous_notes = dict((job.notes or {}).get("autonomous") or {})
                artifact_draft_payload = autonomous_notes.get("artifact_draft")
                artifact_draft = ArtifactDraft.model_validate(artifact_draft_payload) if artifact_draft_payload else None
                rendered = self.runtime.documents.build_application_artifacts(job_model, facts, artifact_draft=artifact_draft)
            except ValueError as exc:
                rendered = []
                blocking_artifact_failures = [item.strip() for item in str(exc).split(';') if item.strip()]

            artifacts_by_kind: dict[str, str] = {}
            for artifact in rendered:
                artifact_enum = self._artifact_kind(artifact.kind, artifact.path.name)
                artifacts_by_kind[artifact_enum.value] = str(artifact.path)
                app_repo.store_artifact(
                    artifact_enum,
                    str(artifact.path),
                    artifact.content_hash,
                    artifact.validation_results,
                    job_posting_id=job.id,
                    application_id=application.id,
                )

            for question_record, question in stored_questions:
                memory_context = self._answer_memory_context(question, job)
                answer_memory = [
                    {
                        "canonical_question": memory.canonical_question,
                        "answer_text": memory.answer_text,
                        "grounded_fact_ids": memory.grounded_fact_ids,
                        "approved": memory.approved,
                        "context_constraints": memory.context_constraints,
                    }
                    for memory in app_repo.find_answer_memory(
                        question.normalized_key or self.runtime.grounding.canonicalize_question(question.prompt_text),
                        context_constraints=memory_context,
                    )
                ]
                answer = self._artifact_answer_for_question(question, artifacts_by_kind)
                if answer is None:
                    answer = await self.runtime.grounding.answer_question(
                        question.prompt_text,
                        facts,
                        options=question.options,
                        normalized_key=question.normalized_key,
                        answer_memory=answer_memory,
                        memory_context=memory_context,
                    )
                app_repo.store_answer(question_record.id, answer)
                if (
                    answer.canonical_question
                    and answer.verification_status == VerificationStatus.VERIFIED
                    and not question.sensitive
                    and question.question_type.value == "deterministic"
                ):
                    app_repo.store_answer_memory(answer.canonical_question, answer, approved=True, context_constraints=memory_context)

            app_service = ApplicationService(job_repo, app_repo)
            question_answers = app_repo.list_answers_for_application(application.id)
            missing_required = app_service.missing_required_questions(question_answers)
            ungrounded = app_service.ungrounded_question_prompts(question_answers)
            low_confidence = app_service.low_confidence_answers(question_answers)
            artifacts = list(app_repo.list_artifacts(application_id=application.id))
            artifact_kinds = {artifact.kind for artifact in artifacts}
            artifact_kinds.add(ArtifactKind.REVIEW_PACKET)
            artifact_validation_failures = self._merge_flags(blocking_artifact_failures + app_service.artifact_validation_failures(artifacts))
            artifact_validation_warnings = app_service.artifact_validation_warnings(artifacts)
            duplicate_siblings = [
                {
                    "job_id": sibling.id,
                    "source": sibling.source_adapter,
                    "title": sibling.title,
                    "status": sibling.lifecycle_status.value,
                }
                for sibling in job_repo.duplicate_siblings(job.duplicate_cluster_key, exclude_job_id=job.id)
            ]
            sensitive_questions = [question.prompt_text for _record, question in stored_questions if question.sensitive]
            gate = app_service.submission_gate(
                job,
                application,
                artifact_kinds,
                ungrounded,
                self._policy_for_source(job.source_kind),
                missing_required_fields=missing_required,
                artifact_validation_failures=artifact_validation_failures,
                low_confidence_answers=low_confidence,
                warnings=artifact_validation_warnings,
            )
            packet = ReviewPacket(
                job_id=job.id,
                application_id=application.id,
                qualification=qualification_for_job(job_model, self.build_query(), facts),
                source_policy=self._policy_for_source(job.source_kind),
                artifacts=list(artifacts_by_kind.values()),
                artifact_validation_failures=artifact_validation_failures + artifact_validation_warnings,
                questions=[question for _record, question in stored_questions],
                answers=[self._answer_model(question, answer) for question, answer in question_answers],
                sensitive_questions=sensitive_questions,
                duplicate_cluster_siblings=duplicate_siblings,
                submit_ready=gate.is_ready,
                handoff_url=extraction.handoff_url or adapter.manual_handoff_url(job_model),
            )
            packet_path = self.runtime.config.artifacts_dir(self.runtime.workspace) / f"review-{job.id}.json"
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet_path.write_text(packet.model_dump_json(indent=2), encoding="utf-8")
            app_repo.store_artifact(
                ArtifactKind.REVIEW_PACKET,
                str(packet_path),
                hash_content(packet.model_dump(mode="json")),
                {
                    "submit_ready": gate.is_ready,
                    "valid": not artifact_validation_failures,
                    "failure_reason": "; ".join(artifact_validation_failures),
                    "warnings": artifact_validation_warnings,
                },
                job_posting_id=job.id,
                application_id=application.id,
            )

            review_flags = self._merge_flags(
                gate.missing_required_fields
                + gate.ungrounded_answers
                + gate.low_confidence_answers
                + sensitive_questions
            )
            full_auto_enabled = (
                run.mode == ApplicationMode.AUTO_SUBMIT
                and self.runtime.config.autonomous.review_mode != "manual"
                and gate.is_ready
            )
            if full_auto_enabled:
                application.status = JobLifecycleStatus.APPROVED_FOR_SUBMIT
                application.review_status = ReviewStatus.APPROVED
                application.review_flags = review_flags
            elif gate.is_ready:
                application = app_repo.mark_prepared(application.id, flags=review_flags)
                application.review_status = ReviewStatus.PENDING
            else:
                application.status = JobLifecycleStatus.NEEDS_USER_INPUT
                application.review_status = ReviewStatus.NEEDS_USER_INPUT
                application.review_flags = review_flags
            job.lifecycle_status = application.status
            audit_payload = {
                "status": application.status.value,
                "submit_ready": gate.is_ready,
                "missing_required_fields": gate.missing_required_fields,
                "ungrounded_answers": gate.ungrounded_answers,
                "low_confidence_answers": gate.low_confidence_answers,
                "warnings": gate.warnings,
            }
            if artifact_validation_failures:
                audit_repo.emit(
                    "application.prepare_blocked",
                    "application",
                    application.id,
                    run_id=run_id,
                    task_id=task_id,
                    payload=audit_payload,
                )
            audit_repo.emit(
                "application.prepared",
                "application",
                application.id,
                run_id=run_id,
                task_id=task_id,
                payload=audit_payload,
            )
            run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"status": application.status.value})

    async def _process_submit_task(self, run_id: str, task_id: str, payload: dict[str, Any]) -> None:
        application_id = str(payload["application_id"])
        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)
            audit_repo = AuditRepository(session)
            application = app_repo.get_application(application_id)
            if application is None:
                raise ValueError(f"Application not found: {application_id}")
            job = job_repo.get_job(application.job_posting_id)
            if job is None:
                raise ValueError(f"Job not found for application: {application_id}")
            adapter = self.source_adapters().get(job.source_adapter)
            if adapter is None:
                raise ValueError(f"No adapter configured for {job.source_adapter}")
            settings = self.runtime.config.sources.get(job.source_adapter)
            log.info("  [submit] Checking submit eligibility: submit_enabled=%s, supports_auto_submit=%s",
                     settings.submit_enabled if settings else False,
                     adapter.capabilities().supports_auto_submit if adapter else False)
            if not settings or not settings.submit_enabled or not adapter.capabilities().supports_auto_submit:
                message = f"Auto-submit disabled for source: {job.source_adapter}"
                log.warning("  [submit] BLOCKED: %s", message)
                application.status = JobLifecycleStatus.READY_FOR_REVIEW
                application.review_status = ReviewStatus.MANUAL_HANDOFF
                application.review_flags = self._merge_flags([message])
                job.lifecycle_status = application.status
                audit_repo.emit(
                    "application.submit_blocked",
                    "application",
                    application.id,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"reason": message},
                )
                run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"status": application.status.value, "reason": message})
                return

            app_service = ApplicationService(job_repo, app_repo)
            question_answers = app_repo.list_answers_for_application(application.id)
            artifacts = app_repo.list_artifacts(application_id=application.id)
            artifact_kinds = {artifact.kind for artifact in artifacts}
            artifact_paths = {artifact.kind.value: artifact.path for artifact in artifacts}
            artifact_validation_failures = app_service.artifact_validation_failures(artifacts)
            job_model = self._job_model(job)
            plan = adapter.bind_answers(job_model, question_answers, artifact_paths)
            missing_required = app_service.missing_required_questions(question_answers)
            ungrounded = app_service.ungrounded_question_prompts(question_answers)
            low_confidence = app_service.low_confidence_answers(question_answers)
            gate = app_service.submission_gate(
                job,
                application,
                artifact_kinds,
                ungrounded,
                self._policy_for_source(job.source_kind),
                missing_required_fields=self._merge_flags(missing_required + plan.missing_required_fields),
                artifact_validation_failures=artifact_validation_failures,
                low_confidence_answers=low_confidence,
            )
            log.info("  [submit] Submission gate: is_ready=%s, missing_required=%d, ungrounded=%d, low_confidence=%d",
                     gate.is_ready, len(gate.missing_required_fields), len(gate.ungrounded_answers), len(gate.low_confidence_answers))
            if not gate.is_ready:
                flags = self._merge_flags(gate.missing_required_fields + gate.ungrounded_answers + gate.low_confidence_answers)
                log.warning("  [submit] BLOCKED (gate not ready): missing=%s, ungrounded=%s, low_confidence=%s",
                            gate.missing_required_fields[:3], gate.ungrounded_answers[:3], gate.low_confidence_answers[:3])
                application.status = JobLifecycleStatus.NEEDS_USER_INPUT
                application.review_status = ReviewStatus.NEEDS_USER_INPUT
                application.review_flags = flags
                job.lifecycle_status = application.status
                audit_repo.emit(
                    "application.submit_blocked",
                    "application",
                    application.id,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "missing_required_fields": gate.missing_required_fields,
                        "ungrounded_answers": gate.ungrounded_answers,
                        "low_confidence_answers": gate.low_confidence_answers,
                    },
                )
                run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"status": application.status.value})
                return

            output_dir = self.runtime.config.snapshots_dir(self.runtime.workspace) / application.id
            application.status = JobLifecycleStatus.SUBMITTING
            job.lifecycle_status = JobLifecycleStatus.SUBMITTING
            submit_url = str(job.apply_url or job.posting_url or "unknown")
            log.info("  [submit] Launching browser submission to: %s", submit_url)

        result = await adapter.submit(job_model, plan, output_dir)
        log.info("  [submit] Browser result: status=%s, submitted=%s, uncertain=%s, message=%s",
                 result.status.value, result.submitted, result.uncertain, (result.message or "")[:100])

        # Retry with exponential backoff if the failure is classified as retryable
        retry_cfg = self.runtime.config.retry
        if not result.submitted and result.evidence:
            failure_info = classify_submission_failure(result.evidence)
            if failure_info.get("retryable"):
                dom_path = result.evidence.dom_snapshot_path or result.evidence.post_submit_dom_snapshot_path
                dom_analysis = None
                if dom_path:
                    try:
                        dom_html = Path(dom_path).read_text(encoding="utf-8", errors="replace")
                        dom_analysis = analyze_dom_snapshot(dom_html)
                    except Exception as exc:
                        log.warning("  [submit] Failed to analyze DOM snapshot at %s: %s", dom_path, exc)
                        dom_analysis = None

                should_retry = True
                if dom_analysis:
                    if dom_analysis.get("has_login_wall") or dom_analysis.get("has_captcha"):
                        should_retry = False
                        log.warning("  [submit] Login wall or CAPTCHA detected, not retrying")
                    if dom_analysis.get("has_confirmation"):
                        should_retry = False
                        log.info("  [submit] Confirmation page detected, not retrying")

                if should_retry:
                    import anyio
                    for retry_attempt in range(1, retry_cfg.max_retries + 1):
                        wait = min(retry_cfg.backoff_base ** retry_attempt, retry_cfg.backoff_max)
                        log.info("  [submit] Retry %d/%d after %.1fs...", retry_attempt, retry_cfg.max_retries, wait)
                        await anyio.sleep(wait)
                        result = await adapter.submit(job_model, plan, output_dir)
                        log.info("  [submit] Retry %d result: status=%s, submitted=%s",
                                 retry_attempt, result.status.value, result.submitted)
                        if result.submitted:
                            break
                        # Check if the new failure is non-retryable
                        if result.evidence:
                            new_failure = classify_submission_failure(result.evidence)
                            if not new_failure.get("retryable"):
                                log.info("  [submit] Non-retryable failure on retry %d, stopping", retry_attempt)
                                break

        with self.runtime.session_scope() as session:
            run_repo = RunRepository(session)
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)
            audit_repo = AuditRepository(session)
            application = app_repo.get_application(application_id)
            if application is None:
                raise ValueError(f"Application not found: {application_id}")
            job = job_repo.get_job(application.job_posting_id)
            if job is None:
                raise ValueError(f"Job not found for application: {application_id}")
            application = app_repo.mark_submission_result(application.id, result.status)
            job.lifecycle_status = result.status
            if result.evidence and result.evidence.pre_submit_snapshot_path:
                app_repo.store_artifact(
                    ArtifactKind.SNAPSHOT,
                    result.evidence.pre_submit_snapshot_path,
                    hash_content(result.evidence.pre_submit_snapshot_path),
                    {"phase": "pre_submit", "valid": True},
                    job_posting_id=job.id,
                    application_id=application.id,
                )
            if result.evidence and result.evidence.dom_snapshot_path:
                app_repo.store_artifact(
                    ArtifactKind.SNAPSHOT,
                    result.evidence.dom_snapshot_path,
                    hash_content(result.evidence.dom_snapshot_path),
                    {"phase": "dom_pre_submit", "failure_reason": result.evidence.failure_reason, "valid": True},
                    job_posting_id=job.id,
                    application_id=application.id,
                )
            if result.evidence and result.evidence.post_submit_dom_snapshot_path:
                app_repo.store_artifact(
                    ArtifactKind.SNAPSHOT,
                    result.evidence.post_submit_dom_snapshot_path,
                    hash_content(result.evidence.post_submit_dom_snapshot_path),
                    {"phase": "dom_post_submit", "failure_reason": result.evidence.failure_reason, "valid": True},
                    job_posting_id=job.id,
                    application_id=application.id,
                )
            receipt_path = result.snapshot_path or (result.evidence.final_snapshot_path if result.evidence else None)
            if receipt_path:
                app_repo.store_artifact(
                    ArtifactKind.SUBMISSION_RECEIPT,
                    receipt_path,
                    hash_content(receipt_path),
                    {"message": result.message},
                    job_posting_id=job.id,
                    application_id=application.id,
                )
            trace_path = result.trace_path or (result.evidence.trace_path if result.evidence else None)
            if trace_path:
                app_repo.store_artifact(
                    ArtifactKind.SUBMISSION_TRACE,
                    trace_path,
                    hash_content(trace_path),
                    {"message": result.message},
                    job_posting_id=job.id,
                    application_id=application.id,
                )
            app_repo.record_submit_attempt(
                application.id,
                result.status.value,
                self._policy_for_source(job.source_kind),
                self._submit_attempt_payload(result),
                snapshot_path=receipt_path,
            )
            classification = classify_job(
                source_kind=job.source_kind,
                apply_url=job.apply_url,
                posting_url=job.posting_url,
                source_adapter=job.source_adapter,
            )
            submit_payload = result.model_dump(mode="json")
            submit_payload["automation_metadata"] = {
                "board_family": classification.board_family.value,
                "automation_tier": classification.automation_tier.value,
                "supports_auto_submit": classification.supports_auto_submit,
                "detection_method": classification.detection_method,
            }
            audit_repo.emit(
                "application.submitted",
                "application",
                application.id,
                run_id=run_id,
                task_id=task_id,
                payload=submit_payload,
            )
            run_repo.finish_task(task_id, TaskStatus.COMPLETED, checkpoint_state={"status": result.status.value})

    def _adapter_for_single_board(self, adapter_name: str, board: str) -> SourceAdapter:
        if adapter_name == "greenhouse":
            return GreenhouseAdapter([board])
        if adapter_name == "lever":
            return LeverAdapter([board])
        if adapter_name == "ashby":
            return AshbyAdapter([board])
        adapter = self.source_adapters().get(adapter_name)
        if adapter is not None and board in adapter.boards:
            return adapter
        raise ValueError(f"Unknown adapter: {adapter_name}")

    def _policy_for_source(self, source_kind: str) -> PolicyMode:
        return resolve_source_policy(source_kind, self.runtime.config.policy, self.runtime.config.autonomous)

    def _artifact_kind(self, artifact_kind: str, filename: str) -> ArtifactKind:
        if artifact_kind == "context":
            return ArtifactKind.SNAPSHOT
        if filename.endswith("resume.txt"):
            return ArtifactKind.RESUME_TEXT
        if filename.endswith("cover_letter.txt"):
            return ArtifactKind.COVER_LETTER_TEXT
        if "resume" in filename and filename.endswith(".pdf"):
            return ArtifactKind.RESUME_PDF
        if "cover_letter" in filename and filename.endswith(".pdf"):
            return ArtifactKind.COVER_LETTER_PDF
        return ArtifactKind.SNAPSHOT

    def _job_model(self, job: JobPosting):
        from findmyjob.core.types import NormalizedJobPosting

        return NormalizedJobPosting(
            company_name=job.company.display_name,
            company_key=job.company.normalized_name,
            title=job.title,
            source=job.source_adapter,
            source_kind=job.source_kind,
            source_job_id=job.source_job_id,
            posting_url=job.posting_url,
            apply_url=job.apply_url,
            location_raw=job.location_raw,
            location_normalized=job.location_normalized,
            country_code=job.country_code,
            region_code=job.region_code,
            city=job.city,
            location_scope=job.location_scope,
            workplace_type=job.workplace_type,
            employment_type=job.employment_type,
            experience_level=job.experience_level,
            posted_at=job.posted_at,
            source_updated_at=job.source_updated_at,
            compensation=job.compensation,
            compensation_min=job.compensation_min,
            compensation_max=job.compensation_max,
            compensation_currency=job.compensation_currency,
            compensation_interval=job.compensation_interval,
            remote_country_codes=list(job.remote_country_codes or []),
            company_employee_count_min=job.company.employee_count_min,
            company_employee_count_max=job.company.employee_count_max,
            company_size_bucket=job.company.company_size_bucket,
            metadata_quality=dict(job.metadata_quality or {}),
            description=job.description,
            normalized_description=job.normalized_description,
            discovered_at=job.discovered_at,
            job_identity_key=job.job_identity_key,
            duplicate_cluster_key=job.duplicate_cluster_key,
            lifecycle_status=job.lifecycle_status,
            notes={**(job.notes or {}), **self._source_runtime_notes(job.source_adapter)},
        )

    def _fact_model(self, record) -> ProfileFact:
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

    def _question_model(self, record: ApplicationQuestionRecord) -> ApplicationQuestion:
        field_config = record.field_config or {}
        return ApplicationQuestion(
            source_field_name=record.source_field_name,
            prompt_text=record.prompt_text,
            normalized_key=record.normalized_key,
            question_type=record.question_type,
            widget_type=record.widget_type,
            section=field_config.get("section"),
            step_id=field_config.get("step_id"),
            required=record.required,
            input_role=str(field_config.get("input_role") or "data"),
            visible_to_operator=bool(field_config.get("visible_to_operator", True)),
            options=list(record.options or []),
            option_details=list(field_config.get("option_details") or []),
            sensitive=bool(field_config.get("sensitive")),
            file_constraints=dict(field_config.get("file_constraints") or {}),
            submission_binding=dict(field_config.get("submission_binding") or {}),
            source_confidence=float(field_config.get("source_confidence") or 1.0),
            source_snapshot_ref=record.source_snapshot_ref,
        )

    def _answer_model(self, question_record: ApplicationQuestionRecord, answer_record: ApplicationAnswerRecord | None) -> GroundedAnswer:
        question = self._question_model(question_record)
        artifact_binding = None
        if answer_record is not None and answer_record.binding_payload:
            artifact_binding = ArtifactBinding.model_validate(answer_record.binding_payload)
        if answer_record is None:
            return GroundedAnswer(question=question.prompt_text, question_type=question.question_type)
        return GroundedAnswer(
            question=question.prompt_text,
            question_type=question.question_type,
            answer=answer_record.candidate_answer,
            used_fact_ids=list(answer_record.grounded_fact_ids or []),
            provenance=answer_record.provenance,
            canonical_question=answer_record.answer_source,
            artifact_binding=artifact_binding,
            verification_status=answer_record.verification_status,
        )

    def _artifact_answer_for_question(self, question: ApplicationQuestion, artifacts_by_kind: dict[str, str]) -> GroundedAnswer | None:
        kinds = self._document_artifact_kinds(question)
        if kinds is None:
            return None
        preferred_file, fallback_file, text_kind = kinds
        if question.question_type == QuestionType.FILE:
            return self._file_answer_for_question(question, artifacts_by_kind, preferred_file, fallback_file)
        if str(question.widget_type or '').strip().lower() not in {'textarea', 'text'}:
            return None
        text_path = artifacts_by_kind.get(text_kind.value)
        text_value = self._artifact_text_content(text_path)
        if not text_value:
            return None
        return GroundedAnswer(
            question=question.prompt_text,
            question_type=question.question_type,
            answer=text_value,
            provenance='artifact',
            canonical_question=question.normalized_key,
            artifact_binding=ArtifactBinding(
                artifact_kind=text_kind,
                source_artifact_kind=text_kind,
                path=text_path,
                mime_type='text/plain',
            ),
            verification_status=VerificationStatus.VERIFIED,
        )

    def _file_answer_for_question(
        self,
        question: ApplicationQuestion,
        artifacts_by_kind: dict[str, str],
        preferred: ArtifactKind,
        fallback: ArtifactKind,
    ) -> GroundedAnswer:
        artifact_path = artifacts_by_kind.get(preferred.value) or artifacts_by_kind.get(fallback.value)
        if not artifact_path:
            return GroundedAnswer(
                question=question.prompt_text,
                question_type=question.question_type,
                canonical_question=question.normalized_key,
                verification_status=VerificationStatus.NEEDS_USER_INPUT,
            )
        bound_kind = preferred if artifact_path.endswith('.pdf') else fallback
        mime_type = 'application/pdf' if artifact_path.endswith('.pdf') else 'text/plain'
        return GroundedAnswer(
            question=question.prompt_text,
            question_type=question.question_type,
            answer=artifact_path,
            provenance='artifact',
            canonical_question=question.normalized_key,
            artifact_binding=ArtifactBinding(
                artifact_kind=bound_kind,
                source_artifact_kind=preferred,
                path=artifact_path,
                mime_type=mime_type,
            ),
            verification_status=VerificationStatus.VERIFIED,
        )

    def _document_artifact_kinds(self, question: ApplicationQuestion) -> tuple[ArtifactKind, ArtifactKind, ArtifactKind] | None:
        lowered = ' '.join(
            value
            for value in [
                str(question.prompt_text or '').strip().lower(),
                str(question.normalized_key or '').strip().lower(),
                str(question.source_field_name or '').strip().lower(),
            ]
            if value
        )
        if 'cover' in lowered and 'letter' in lowered:
            return (ArtifactKind.COVER_LETTER_PDF, ArtifactKind.COVER_LETTER_TEXT, ArtifactKind.COVER_LETTER_TEXT)
        if any(token in lowered for token in {'resume', 'resume/cv', 'curriculum vitae'}) or lowered.endswith(' cv') or lowered == 'cv':
            return (ArtifactKind.RESUME_PDF, ArtifactKind.RESUME_TEXT, ArtifactKind.RESUME_TEXT)
        return None

    @staticmethod
    def _artifact_text_content(path_value: str | None) -> str | None:
        path = str(path_value or '').strip()
        if not path:
            return None
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return None
        return text or None

    def _source_runtime_notes(self, source_adapter: str) -> dict[str, Any]:
        settings = self.runtime.config.sources.get(source_adapter)
        if settings is None:
            return {}
        captcha_cfg = self.runtime.config.captcha
        return {
            "submit_enabled": settings.submit_enabled,
            "browser_timeout_seconds": settings.browser_timeout_seconds,
            "browser_attach_enabled": settings.browser_attach_enabled,
            "browser_cdp_url": settings.browser_cdp_url,
            "browser_mode": self.runtime.config.autonomous.browser_mode,
            "max_open_tabs": self.runtime.config.autonomous.max_open_tabs,
            "persist_mode": self.runtime.config.autonomous.persist_mode,
            "review_mode": self.runtime.config.autonomous.review_mode,
            "live_smoke_urls": list(settings.live_smoke_urls),
            "captcha_strategy": captcha_cfg.strategy,
            "captcha_provider": captcha_cfg.provider,
            "captcha_api_key_env": captcha_cfg.api_key_env,
            "captcha_solve_timeout": captcha_cfg.solve_timeout_seconds,
            "capture_policy": {
                "traces": self.runtime.config.privacy.traces.value,
                "dom_snapshots": self.runtime.config.privacy.dom_snapshots.value,
                "screenshots": self.runtime.config.privacy.screenshots.value,
            },
        }

    def _submit_attempt_payload(self, result) -> dict[str, Any]:
        if self.runtime.config.privacy.submit_evidence == SubmitEvidenceMode.FULL:
            payload = result.model_dump(mode="json")
            if result.evidence:
                payload["failure_classification"] = classify_submission_failure(result.evidence)
        else:
            evidence = result.evidence
            field_audit = list(evidence.field_audit or []) if evidence is not None else []
            payload = {
                "status": result.status.value,
                "submitted": result.submitted,
                "uncertain": result.uncertain,
                "message": result.message,
                "snapshot_path": result.snapshot_path,
                "trace_path": result.trace_path,
                "plan": {
                    "source_kind": result.plan.source_kind if result.plan is not None else None,
                    "application_url": result.plan.application_url if result.plan is not None else None,
                    "field_count": len(result.plan.fields) if result.plan is not None else 0,
                    "missing_required_fields": list(result.plan.missing_required_fields) if result.plan is not None else [],
                    "notes": list(result.plan.notes) if result.plan is not None else [],
                },
                "evidence": {
                    "pre_submit_snapshot_path": evidence.pre_submit_snapshot_path if evidence is not None else None,
                    "final_snapshot_path": evidence.final_snapshot_path if evidence is not None else None,
                    "trace_path": evidence.trace_path if evidence is not None else None,
                    "dom_snapshot_path": evidence.dom_snapshot_path if evidence is not None else None,
                    "post_submit_dom_snapshot_path": evidence.post_submit_dom_snapshot_path if evidence is not None else None,
                    "confirmation_text": evidence.confirmation_text if evidence is not None else None,
                    "confirmation_strategy": evidence.confirmation_strategy if evidence is not None else None,
                    "failure_reason": evidence.failure_reason if evidence is not None else None,
                    "network_errors": list(evidence.network_errors) if evidence is not None else [],
                    "final_url": evidence.final_url if evidence is not None else None,
                    "visible_validation_errors": list(evidence.visible_validation_errors) if evidence is not None else [],
                    "matched_confirmation_markers": list(evidence.matched_confirmation_markers) if evidence is not None else [],
                    "missing_required_controls": list(evidence.missing_required_controls) if evidence is not None else [],
                    "submit_button_present": evidence.submit_button_present if evidence is not None else None,
                    "submit_button_enabled": evidence.submit_button_enabled if evidence is not None else None,
                    "field_audit": [
                        {
                            "field": row.get("field"),
                            "prompt": row.get("prompt"),
                            "status": row.get("status"),
                            "failure_reason": row.get("failure_reason"),
                            "error_text": row.get("error_text"),
                            "value_summary": row.get("value_summary"),
                        }
                        for row in field_audit
                    ],
                },
            }
        if result.evidence and "failure_classification" not in payload:
            payload["failure_classification"] = classify_submission_failure(result.evidence)
        return redact_data(payload, mode=self.runtime.config.privacy.log_redaction)

    def _answer_memory_context(self, question: ApplicationQuestion, job: JobPosting) -> dict[str, Any]:
        options = [option.strip().lower() for option in question.options if option]
        return {
            "question_type": question.question_type.value,
            "source_adapter": job.source_adapter,
            "option_signature": "|".join(sorted(options)),
        }

    def _merge_flags(self, flags: list[str]) -> list[str]:
        seen: list[str] = []
        for flag in flags:
            cleaned = str(flag).strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen





















