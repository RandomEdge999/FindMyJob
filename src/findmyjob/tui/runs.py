from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Static

from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import GreenhouseBenchmarkSummary, SmokeTestResult
from findmyjob.db.models import AuditEventRecord
from findmyjob.db.repositories import RunRepository
from findmyjob.tui.common import render_lines


class RunsView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="runs-view")
        self.runs_table = DataTable(id="runs-table")
        self.tasks_table = DataTable(id="run-tasks-table")
        self.detail = Static(id="runs-detail")
        self.smoke_table = DataTable(id="runs-smoke-table")
        self.benchmark_table = DataTable(id="runs-benchmark-table")
        self.ops_summary = Static(id="runs-ops-summary")
        self.ops_detail = Static(id="runs-ops-detail")
        self.run_ids: list[str] = []
        self.smoke_results: list[SmokeTestResult] = []
        self.benchmark_summaries: list[GreenhouseBenchmarkSummary] = []
        self.run_detail_text = ""
        self.ops_summary_text = ""
        self.ops_detail_text = ""
        self._runtime: AppRuntime | None = None

    def compose(self) -> ComposeResult:
        yield Static("Runs & Release Ops", classes="screen-title")
        yield Horizontal(
            Button("Refresh", id="runs-refresh"),
            Button("Inspect Run", id="runs-inspect"),
            Button("Inspect Smoke", id="runs-inspect-smoke"),
            Button("Inspect Benchmark", id="runs-inspect-benchmark"),
            classes="action-row",
        )
        yield self.ops_summary
        yield Static("Recent Runs", classes="section-title")
        yield self.runs_table
        yield Static("Tasks", classes="section-title")
        yield self.tasks_table
        yield Static("Run Detail", classes="section-title")
        yield self.detail
        yield Static("Smoke Results", classes="section-title")
        yield self.smoke_table
        yield Static("Benchmark History", classes="section-title")
        yield self.benchmark_table
        yield Static("Release Detail", classes="section-title")
        yield self.ops_detail

    def on_mount(self) -> None:
        self.runs_table.add_columns("Run", "Type", "Status", "Started", "Completed")
        self.tasks_table.add_columns("Task", "Status", "Attempts", "Error")
        self.smoke_table.add_columns("Checked", "Board", "Job", "Mode", "Status", "Reason", "Application")
        self.benchmark_table.add_columns("Run", "Status", "Boards", "Jobs", "Enriched", "429s", "Failures", "Duration", "Throughput")

    def current_run_id(self) -> str | None:
        if not self.run_ids:
            return None
        try:
            row = self.runs_table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.run_ids):
            row = 0
        return self.run_ids[row]

    def current_smoke_result(self) -> SmokeTestResult | None:
        if not self.smoke_results:
            return None
        try:
            row = self.smoke_table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.smoke_results):
            row = 0
        return self.smoke_results[row]

    def current_benchmark_summary(self) -> GreenhouseBenchmarkSummary | None:
        if not self.benchmark_summaries:
            return None
        try:
            row = self.benchmark_table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.benchmark_summaries):
            row = 0
        return self.benchmark_summaries[row]

    def refresh_view(self, runtime: AppRuntime) -> None:
        self._runtime = runtime
        self.runs_table.clear(columns=False)
        self.tasks_table.clear(columns=False)
        self.smoke_table.clear(columns=False)
        self.benchmark_table.clear(columns=False)
        self.run_ids = []
        self.smoke_results = runtime.list_smoke_results(limit=25)
        self.benchmark_summaries = runtime.list_benchmark_summaries(limit=15)
        with runtime.session_scope() as session:
            runs = RunRepository(session).list_runs(limit=25)
            for run in runs:
                self.run_ids.append(run.id)
                self.runs_table.add_row(run.id[:12], run.run_type, run.status.value, str(run.started_at or ""), str(run.completed_at or ""))
        for result in self.smoke_results:
            checked_at = result.checked_at.isoformat() if result.checked_at is not None else ""
            mode = "confirmed_submit" if result.submit_confirmed else "check_only"
            self.smoke_table.add_row(
                checked_at,
                result.board_token,
                result.source_job_id,
                mode,
                result.status,
                result.failure_reason or "",
                result.application_id or "",
            )
        for summary in self.benchmark_summaries:
            self.benchmark_table.add_row(
                summary.run_id[:12],
                summary.status,
                f"{summary.boards_succeeded}/{summary.boards_attempted}",
                str(summary.jobs_seen),
                str(summary.jobs_enriched),
                str(summary.rate_limited_count),
                str(summary.failure_count),
                f"{summary.duration_seconds:.2f}s",
                f"{summary.jobs_per_minute:.2f} jobs/min",
            )
        self.ops_summary_text = self._build_ops_summary()
        self.ops_summary.update(self.ops_summary_text)
        if self.run_ids:
            self.show_detail(runtime, self.run_ids[0])
        else:
            self.run_detail_text = "No runs recorded."
            self.detail.update(self.run_detail_text)
        if self.smoke_results:
            self.show_smoke_detail(self.smoke_results[0])
        elif self.benchmark_summaries:
            self.show_benchmark_detail(self.benchmark_summaries[0])
        else:
            self.ops_detail_text = "No smoke or benchmark history recorded."
            self.ops_detail.update(self.ops_detail_text)

    def show_detail(self, runtime: AppRuntime, run_id: str) -> None:
        self.tasks_table.clear(columns=False)
        with runtime.session_scope() as session:
            run = RunRepository(session).get_run(run_id)
            if run is None:
                self.run_detail_text = "Run not found."
                self.detail.update(self.run_detail_text)
                return
            tasks = RunRepository(session).list_tasks(run_id)
            for task in tasks[:25]:
                self.tasks_table.add_row(task.task_type, task.status.value, str(task.attempt_count), task.last_error or "")
            events = session.execute(
                select(AuditEventRecord)
                .where(AuditEventRecord.run_id == run_id)
                .order_by(AuditEventRecord.created_at.desc())
                .limit(8)
            ).scalars().all()
            checkpoint = run.checkpoint_state or {}
            self.run_detail_text = render_lines(
                [
                    f"Run: {run.id}",
                    f"Type: {run.run_type} | Status: {run.status.value} | Mode: {run.mode.value}",
                    f"Started: {run.started_at or '-'} | Completed: {run.completed_at or '-'}",
                    "Checkpoint:",
                    *[f"{key}: {value}" for key, value in checkpoint.items()],
                    "Recent Events:",
                    *[f"- {event.event_type} | {event.entity_type} | {event.created_at}" for event in events],
                ]
            )
            self.detail.update(self.run_detail_text)

    def show_current_smoke_detail(self) -> None:
        result = self.current_smoke_result()
        if result is None:
            self.ops_detail_text = "No smoke results recorded."
            self.ops_detail.update(self.ops_detail_text)
            return
        self.show_smoke_detail(result)

    def show_current_benchmark_detail(self) -> None:
        summary = self.current_benchmark_summary()
        if summary is None:
            self.ops_detail_text = "No benchmark runs recorded."
            self.ops_detail.update(self.ops_detail_text)
            return
        self.show_benchmark_detail(summary)

    def focus_smoke_history(self) -> None:
        try:
            self.smoke_table.focus()
        except Exception:
            pass
        self.show_current_smoke_detail()

    def focus_benchmark_history(self) -> None:
        try:
            self.benchmark_table.focus()
        except Exception:
            pass
        self.show_current_benchmark_detail()

    def show_smoke_detail(self, result: SmokeTestResult) -> None:
        mode = "confirmed_submit" if result.submit_confirmed else "check_only"
        lines = [
            "Smoke Result",
            f"Checked: {result.checked_at.isoformat() if result.checked_at is not None else '-'}",
            f"Board: {result.board_token} | Job: {result.source_job_id}",
            f"Mode: {mode} | Status: {result.status}",
            f"Application: {result.application_id or '-'} | Job Posting: {result.job_posting_id or '-'}",
            f"Apply URL: {result.apply_url or '-'}",
            f"Failure: {result.failure_reason or '-'}",
        ]
        reference_lines = self._reference_lines(result.references)
        if reference_lines:
            lines.extend(["References:", *reference_lines])
        if result.notes:
            lines.extend(["Notes:", *[f"- {note}" for note in result.notes[:5]]])
        self.ops_detail_text = render_lines(lines)
        self.ops_detail.update(self.ops_detail_text)

    def show_benchmark_detail(self, summary: GreenhouseBenchmarkSummary) -> None:
        self.ops_detail_text = render_lines(
            [
                "Benchmark Run",
                f"Run: {summary.run_id}",
                f"Status: {summary.status}",
                f"Boards: {summary.boards_succeeded}/{summary.boards_attempted} succeeded",
                f"Jobs: seen={summary.jobs_seen} | enriched={summary.jobs_enriched} | inactive={summary.inactive_jobs}",
                f"Pressure: failures={summary.failure_count} | 429={summary.rate_limited_count} | requests={summary.request_count}",
                f"Duration: {summary.duration_seconds:.2f}s | Throughput: {summary.jobs_per_minute:.2f} jobs/min",
                f"Started: {summary.started_at or '-'} | Completed: {summary.completed_at or '-'}",
                f"Boards in run: {', '.join(summary.board_tokens) or '-'}",
            ]
        )
        self.ops_detail.update(self.ops_detail_text)

    def _build_ops_summary(self) -> str:
        smoke_counts = Counter(result.status for result in self.smoke_results)
        lines = [
            (
                "Smoke history: "
                f"total={len(self.smoke_results)} | pass={smoke_counts.get('pass', 0)} | "
                f"warning={smoke_counts.get('warning', 0)} | fail={smoke_counts.get('fail', 0)}"
            )
        ]
        if self.benchmark_summaries:
            latest = self.benchmark_summaries[0]
            lines.append(
                "Latest benchmark: "
                f"{latest.status} | boards={latest.boards_succeeded}/{latest.boards_attempted} | jobs={latest.jobs_seen} | "
                f"429={latest.rate_limited_count} | throughput={latest.jobs_per_minute:.2f} jobs/min"
            )
        else:
            lines.append("Latest benchmark: none recorded")
        return render_lines(lines)

    def _reference_lines(self, references: dict[str, object]) -> list[str]:
        if not references:
            return []
        preferred_order = [
            'prepare_run_id',
            'submit_run_id',
            'application_status',
            'review_packet',
            'receipt_path',
            'trace_path',
            'pre_submit_snapshot_path',
            'final_snapshot_path',
            'dom_snapshot_path',
            'post_submit_dom_snapshot_path',
            'final_url',
        ]
        lines: list[str] = []
        seen: set[str] = set()
        for key in preferred_order:
            value = references.get(key)
            if value is None or value == '' or value == []:
                continue
            seen.add(key)
            lines.append(f'- {key}: {value}')
        for key in sorted(references):
            if key in seen:
                continue
            value = references.get(key)
            if value is None or value == '' or value == []:
                continue
            lines.append(f'- {key}: {value}')
        return lines
