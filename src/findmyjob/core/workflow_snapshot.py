"""Unified workflow snapshot / frontend‑ready state layer.

This module provides a single, comprehensive snapshot of the entire workspace
readiness, personal onboarding, training history, and review/apply state.
It is designed to be consumed by a frontend operator dashboard or by the
`fmj workflow` command group.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from findmyjob.core.runtime import (
    AppRuntime,
    collect_release_snapshot,
    collect_support_bundle,
    inspect_personal_rehearsal,
)
from findmyjob.core.types import (
    PersonalRehearsalReport,
    ReleaseSnapshotReport,
    TrainingRunSummary,
)
from findmyjob.personal.training import build_training_report


class WorkflowSnapshot(BaseModel):
    """Unified snapshot of workspace readiness, personal state, and training history."""

    generated_at: datetime
    workspace: str
    workspace_name: str
    config_path: str
    release_snapshot: ReleaseSnapshotReport
    personal_rehearsal: PersonalRehearsalReport | None = None
    training_summary: TrainingRunSummary | None = None
    review_apply_summary: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


def collect_workflow_snapshot(
    workspace: Path | None = None,
    *,
    runtime: AppRuntime | None = None,
    include_personal: bool = True,
    include_training: bool = True,
    include_review_apply: bool = True,
) -> WorkflowSnapshot:
    """Collect a unified snapshot of the entire workspace and operator state.

    Args:
        workspace: Workspace root (defaults to current directory).
        runtime: Pre‑bootstrapped AppRuntime (optional).
        include_personal: Whether to include personal rehearsal data.
        include_training: Whether to include training‑mode history.
        include_review_apply: Whether to include review/apply queue summaries.

    Returns:
        A WorkflowSnapshot containing all requested subsections.
    """
    active_runtime = runtime
    root = runtime.workspace if runtime is not None else (workspace or Path.cwd()).resolve()

    # 1. Release snapshot (doctor, launch‑check, config validation, launch profile, smoke/benchmark)
    release_snapshot = collect_release_snapshot(workspace=root, runtime=active_runtime)

    # 2. Personal rehearsal (onboarding, preferences, inbox, daily run, artifact previews)
    personal_rehearsal: PersonalRehearsalReport | None = None
    if include_personal:
        try:
            personal_rehearsal = inspect_personal_rehearsal(workspace=root, runtime=active_runtime)
        except Exception as exc:
            release_snapshot.notes.append(f"Personal rehearsal unavailable: {exc}")

    # 3. Training‑mode summary (latest training run)
    training_summary: TrainingRunSummary | None = None
    if include_training and active_runtime is not None:
        try:
            from findmyjob.personal.training import build_training_report
            from findmyjob.core.types import GreenhouseTrainingJobSummary, TrainingReviewOutcome
            from datetime import datetime

            with active_runtime.session_scope() as session:
                from findmyjob.db.repositories import RunRepository
                repo = RunRepository(session)
                runs = repo.list_runs_by_type(run_type="training", limit=1)
                if runs:
                    run_id = runs[0].run_id
                    report_dict = build_training_report(active_runtime, run_id=run_id)

                    # Convert dict to TrainingRunSummary
                    sampled_jobs = [
                        GreenhouseTrainingJobSummary.model_validate(job)
                        for job in report_dict.get("sampled_jobs", [])
                    ]
                    reviews = [
                        TrainingReviewOutcome.model_validate(review)
                        for review in report_dict.get("reviews", [])
                    ]
                    # Parse ISO datetime strings
                    started_at = None
                    if report_dict.get("started_at"):
                        started_at = datetime.fromisoformat(report_dict["started_at"])
                    completed_at = None
                    if report_dict.get("completed_at"):
                        completed_at = datetime.fromisoformat(report_dict["completed_at"])

                    training_summary = TrainingRunSummary(
                        run_id=report_dict["run_id"],
                        started_at=started_at,
                        completed_at=completed_at,
                        start_url="https://my.greenhouse.io/jobs",  # default
                        posted_window=10,
                        batch_size=5,
                        cdp_url="http://127.0.0.1:9222",
                        sampled_jobs=sampled_jobs,
                        reviews=reviews,
                        approved_count=report_dict.get("approved_count", 0),
                        rejected_count=report_dict.get("rejected_count", 0),
                        artifact_paths=[],  # not in report dict
                        notes=report_dict.get("notes", []),
                        promoted_application_ids=report_dict.get("promoted_application_ids", []),
                        review_packet_paths=report_dict.get("review_packet_paths", []),
                    )
        except Exception as exc:
            release_snapshot.notes.append(f"Training summary unavailable: {exc}")

    # 4. Review/apply queue summary (pending applications, questions, etc.)
    review_apply_summary: dict[str, Any] | None = None
    if include_review_apply and active_runtime is not None:
        try:
            with active_runtime.session_scope() as session:
                from findmyjob.db.repositories import ApplicationRepository
                app_repo = ApplicationRepository(session)
                pending = app_repo.list_review_queue()
                review_apply_summary = {
                    "pending_applications": len(pending),
                    "applications": [
                        {
                            "application_id": app.id,
                            "job_id": app.job_posting_id,
                            "status": app.status,
                            "created_at": app.created_at,
                        }
                        for app in pending[:10]
                    ],
                }
        except Exception as exc:
            release_snapshot.notes.append(f"Review/apply summary unavailable: {exc}")

    return WorkflowSnapshot(
        generated_at=datetime.now(timezone.utc),
        workspace=str(root),
        workspace_name=root.name,
        config_path=str(release_snapshot.config_path),
        release_snapshot=release_snapshot,
        personal_rehearsal=personal_rehearsal,
        training_summary=training_summary,
        review_apply_summary=review_apply_summary,
        notes=release_snapshot.notes.copy(),
    )