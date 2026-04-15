from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Static

from findmyjob.core.runtime import AppRuntime
from findmyjob.db.models import JobPosting
from findmyjob.db.repositories import ApplicationRepository
from findmyjob.tui.common import render_lines


class ReviewView(Container):
    def __init__(self) -> None:
        super().__init__(id="review-view")
        self.table = DataTable(id="review-table")
        self.detail = Static(id="review-detail")
        self.application_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Review Queue", classes="screen-title")
        yield Horizontal(
            Button("Refresh", id="review-refresh"),
            Button("Approve", id="review-approve"),
            Button("Reject", id="review-reject"),
            Button("Needs Input", id="review-request-input"),
            Button("Handoff", id="review-handoff"),
            classes="action-row",
        )
        yield Horizontal(self.table, self.detail, classes="results-layout")

    def on_mount(self) -> None:
        self.table.add_columns("Application", "Company", "Title", "Status", "Review", "Flags")

    def current_application_id(self) -> str | None:
        if not self.application_ids:
            return None
        try:
            row = self.table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.application_ids):
            row = 0
        return self.application_ids[row]

    def refresh_view(self, runtime: AppRuntime) -> None:
        self.table.clear(columns=False)
        self.application_ids = []
        with runtime.session_scope() as session:
            queue = ApplicationRepository(session).list_review_queue()
            for application in queue[:25]:
                job = session.get(JobPosting, application.job_posting_id)
                if job is None:
                    continue
                self.application_ids.append(application.id)
                self.table.add_row(
                    application.id,
                    job.company.display_name,
                    job.title,
                    application.status.value,
                    application.review_status.value,
                    ", ".join(application.review_flags[:2]),
                )
        if self.application_ids:
            self.show_detail(runtime, self.application_ids[0])
        else:
            self.detail.update("Review queue is empty.")

    def show_detail(self, runtime: AppRuntime, application_id: str) -> None:
        with runtime.session_scope() as session:
            app_repo = ApplicationRepository(session)
            application = app_repo.get_application(application_id)
            if application is None:
                self.detail.update("Application not found.")
                return
            job = session.get(JobPosting, application.job_posting_id)
            if job is None:
                self.detail.update("Associated job not found.")
                return
            questions = app_repo.list_questions(application_id)
            artifacts = app_repo.list_artifacts(application_id=application_id)
            review_packet = next((artifact for artifact in artifacts if artifact.kind.value == "review_packet"), None)
            self.detail.update(
                render_lines(
                    [
                        f"{job.company.display_name} | {job.title}",
                        f"Application: {application.id}",
                        f"Status: {application.status.value} | Review: {application.review_status.value}",
                        f"Flags: {', '.join(application.review_flags) if application.review_flags else '-'}",
                        f"Questions: {len(questions)} | Artifacts: {len(artifacts)}",
                        f"Review Packet: {review_packet.path if review_packet else '-'}",
                        f"Handoff Reason: {application.handoff_reason or '-'}",
                    ]
                )
            )
