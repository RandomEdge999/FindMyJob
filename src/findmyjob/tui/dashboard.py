from __future__ import annotations

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Static

from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import ModelLaunchProfileReport, ReleaseSnapshotReport
from findmyjob.db.board_repository import BoardRepository
from findmyjob.db.models import Company, JobPosting
from findmyjob.db.repositories import ApplicationRepository, RunRepository, SavedSearchRepository
from findmyjob.personal.workflow import build_personal_inbox
from findmyjob.tui.common import render_lines


class DashboardView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="dashboard-view")
        self.jobs_table = DataTable(id="dashboard-jobs-table")
        self.boards_table = DataTable(id="dashboard-boards-table")
        self.review_table = DataTable(id="dashboard-review-table")
        self.personal_table = DataTable(id="dashboard-personal-table")
        self.runs_table = DataTable(id="dashboard-runs-table")
        self.summary = Static(id="dashboard-summary")
        self.summary_text = ""
        self.release_snapshot: ReleaseSnapshotReport | None = None

    def compose(self) -> ComposeResult:
        yield Static("Dashboard", classes="screen-title")
        yield Horizontal(
            Button("Refresh RC", id="dashboard-refresh-rc"),
            Button("Smoke History", id="dashboard-open-smoke"),
            Button("Benchmark History", id="dashboard-open-benchmarks"),
            id="dashboard-actions",
            classes="action-row",
        )
        yield self.summary
        yield Static("Recent Jobs", classes="section-title")
        yield self.jobs_table
        yield Static("Board Registry", classes="section-title")
        yield self.boards_table
        yield Static("Review Queue", classes="section-title")
        yield self.review_table
        yield Static("Personal Inbox", classes="section-title")
        yield self.personal_table
        yield Static("Recent Runs", classes="section-title")
        yield self.runs_table

    def on_mount(self) -> None:
        self.jobs_table.add_columns("Company", "Title", "Source", "Board", "Status")
        self.boards_table.add_columns("Token", "Company", "Validation", "Live", "Last Sync")
        self.review_table.add_columns("Application", "Status", "Review", "Flags")
        self.personal_table.add_columns("Bucket", "Company", "Title", "Priority", "Triage", "Why")
        self.runs_table.add_columns("Run", "Type", "Status", "Started", "Checkpoint")

    def refresh_view(self, runtime: AppRuntime, query_summary: str) -> None:
        self.jobs_table.clear(columns=False)
        self.boards_table.clear(columns=False)
        self.review_table.clear(columns=False)
        self.personal_table.clear(columns=False)
        self.runs_table.clear(columns=False)
        self.release_snapshot = runtime.collect_release_snapshot(smoke_limit=8, benchmark_limit=5)
        inbox = build_personal_inbox(runtime, limit=10)
        with runtime.session_scope() as session:
            jobs = session.execute(
                select(JobPosting, Company)
                .join(Company, JobPosting.company_id == Company.id)
                .order_by(JobPosting.discovered_at.desc())
                .limit(12)
            ).all()
            for job, company in jobs:
                self.jobs_table.add_row(company.display_name, job.title, job.source_adapter, job.board_token or "", job.lifecycle_status.value)

            boards = BoardRepository(session).list_boards("greenhouse", limit=12)
            for board in boards:
                self.boards_table.add_row(
                    board.board_token,
                    board.company_hint or "",
                    board.validation_status,
                    str(board.live_job_count),
                    str(board.last_sync_at or ""),
                )

            queue = ApplicationRepository(session).list_review_queue()[:12]
            for application in queue:
                self.review_table.add_row(
                    application.id,
                    application.status.value,
                    application.review_status.value,
                    ", ".join(application.review_flags[:2]),
                )

            personal_items = [
                *inbox.shortlisted_jobs,
                *inbox.watching_jobs,
                *inbox.new_matching_jobs,
                *inbox.ready_for_review,
                *inbox.needs_user_input,
                *inbox.approved_pending_submit,
            ]
            for item in personal_items[:12]:
                self.personal_table.add_row(
                    item.bucket.replace('_', ' '),
                    item.company,
                    item.title,
                    item.priority_label or '-',
                    item.triage_status,
                    item.explanation_headline or '-',
                )

            runs = RunRepository(session).list_runs(limit=12)
            for run in runs:
                checkpoint = run.checkpoint_state or {}
                summary = ", ".join(f"{key}={value}" for key, value in list(checkpoint.items())[:3])
                self.runs_table.add_row(run.id[:12], run.run_type, run.status.value, str(run.started_at or ""), summary)
            latest_training = next((run for run in runs if run.run_type == "training"), None)

            saved_count = len(SavedSearchRepository(session).list_searches())

        snapshot = self.release_snapshot
        summary_lines = [
            f"Workspace: {snapshot.workspace_name} | Current Search: {query_summary}",
            f"Launch Check: {snapshot.launch_check.overall_status} | blockers={snapshot.launch_check.fail_count} | warnings={snapshot.launch_check.warning_count}",
            self._smoke_summary(snapshot),
            self._benchmark_summary(snapshot),
            *self._launch_profile_lines(snapshot.launch_profile),
            self._training_summary(latest_training),
            f"Saved Searches: {saved_count}",
            f"Personal Inbox: shortlist={len(inbox.shortlisted_jobs)} | watching={len(inbox.watching_jobs)} | new={len(inbox.new_matching_jobs)} | review={len(inbox.ready_for_review)} | needs_input={len(inbox.needs_user_input)} | approved={len(inbox.approved_pending_submit)}",
            f"Jobs: {len(jobs)} shown | Boards: {len(boards)} shown | Review Queue: {len(queue)} | Runs: {len(runs)}",
        ]
        if snapshot.notes:
            summary_lines.append("Snapshot Notes: " + " | ".join(snapshot.notes[:2]))
        self.summary_text = render_lines(summary_lines)
        self.summary.update(self.summary_text)

    def _training_summary(self, latest_training) -> str:
        if latest_training is None:
            return "Latest Training: no recorded training runs"
        checkpoint = latest_training.checkpoint_state or {}
        return (
            "Latest Training: "
            f"{latest_training.status.value} | sampled={len(checkpoint.get('sampled_jobs') or []) or checkpoint.get('sampled_count') or 0} | "
            f"approved={checkpoint.get('approved_count', 0)} | rejected={checkpoint.get('rejected_count', 0)} | "
            f"promoted={len(checkpoint.get('promoted_application_ids') or [])}"
        )

    def _smoke_summary(self, snapshot: ReleaseSnapshotReport) -> str:
        latest = snapshot.latest_smoke_result
        if latest is None:
            return "Latest Smoke: no recorded smoke results"
        mode = "confirmed_submit" if latest.submit_confirmed else "check_only"
        checked_at = latest.checked_at.isoformat() if latest.checked_at is not None else "unknown"
        summary = f"Latest Smoke: {latest.status} | {checked_at} | board={latest.board_token} | job={latest.source_job_id} | mode={mode}"
        if latest.application_id:
            summary += f" | application={latest.application_id}"
        if latest.failure_reason:
            summary += f" | reason={latest.failure_reason}"
        return summary

    def _benchmark_summary(self, snapshot: ReleaseSnapshotReport) -> str:
        latest = snapshot.latest_benchmark
        if latest is None:
            return "Latest Benchmark: no recorded benchmark runs"
        boards = f"{latest.boards_succeeded}/{latest.boards_attempted}"
        return (
            "Latest Benchmark: "
            f"{latest.status} | boards={boards} | jobs={latest.jobs_seen} | enriched={latest.jobs_enriched} | "
            f"failures={latest.failure_count} | 429={latest.rate_limited_count} | "
            f"duration={latest.duration_seconds:.2f}s | throughput={latest.jobs_per_minute:.2f} jobs/min"
        )

    def _launch_profile_lines(self, report: ModelLaunchProfileReport | None) -> list[str]:
        if report is None:
            return ["Launch Profile: unavailable"]
        roles_by_name = {role.role: role for role in report.roles}
        required_parts: list[str] = []
        issues: list[str] = []
        for role_name in report.required_roles:
            role = roles_by_name.get(role_name)
            if role is None or not role.profile_name:
                required_parts.append(f"{role_name}=unbound")
                continue
            part = f"{role_name}={role.profile_name}/{role.transport or '-'}:{role.status}"
            if role.issues:
                part += f" ({role.issues[0]})"
                issues.extend(f"{role.role}: {issue}" for issue in role.issues)
            required_parts.append(part)
        issues.extend(report.risks)
        lines = [
            f"Launch Profile: {report.overall_status} | transport={report.transport_mix} | missing={', '.join(report.missing_required_roles) or 'none'}",
            "Required Roles: " + " | ".join(required_parts or ["none"]),
        ]
        if issues:
            lines.append("Launch Warnings: " + " | ".join(list(dict.fromkeys(issues))[:3]))
        return lines
