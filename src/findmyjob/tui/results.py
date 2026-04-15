from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Input, Static

from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery, PersonalJobMatchExplanation
from findmyjob.db.models import JobPosting, QualificationResultRecord
from findmyjob.db.repositories import ApplicationRepository, PersonalTriageRepository
from findmyjob.db.search import search_jobs
from findmyjob.personal.triage import assess_personal_job
from findmyjob.tui.common import job_compensation, job_location, render_lines


class ResultsView(Container):
    def __init__(self) -> None:
        super().__init__(id="results-view")
        self.summary = Static(id="results-summary")
        self.filter_input = Input(id="results-filter")
        self.sort_input = Input(value="discovered", id="results-sort")
        self.table = DataTable(id="results-table")
        self.detail = Static(id="results-detail")
        self.job_ids: list[str] = []
        self.explanations: dict[str, PersonalJobMatchExplanation] = {}

    def compose(self) -> ComposeResult:
        yield Static("Job Explorer", classes="screen-title")
        yield self.summary
        yield Horizontal(
            self.filter_input,
            self.sort_input,
            Button("Refresh", id="results-refresh"),
            Button("Inspect", id="results-inspect"),
            Button("Shortlist", id="results-shortlist"),
            Button("Dismiss", id="results-dismiss"),
            Button("Archive", id="results-archive"),
            Button("Packet", id="results-packet"),
            Button("Artifacts", id="results-artifacts"),
            Button("Prepare", id="results-prepare"),
            classes="action-row",
        )
        yield Horizontal(self.table, self.detail, classes="results-layout")

    def on_mount(self) -> None:
        self.filter_input.placeholder = "Filter current results"
        self.sort_input.placeholder = "Sort: discovered | title | company | posted | priority"
        self.table.add_columns(
            "Company",
            "Title",
            "Priority",
            "Triage",
            "Source",
            "Board",
            "Location",
            "Scope",
            "Workplace",
            "Exp",
            "Posted",
            "Comp",
            "Status",
            "Sponsor",
        )

    def current_job_id(self) -> str | None:
        if not self.job_ids:
            return None
        try:
            row = self.table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.job_ids):
            row = 0
        return self.job_ids[row]

    def refresh_view(self, runtime: AppRuntime, query: JobSearchQuery) -> int:
        self.summary.update(f"Active Filters\n{query.summary()}")
        filter_text = self.filter_input.value.strip().lower()
        sort_key = self.sort_input.value.strip().lower() or "discovered"
        rows: list[dict[str, Any]] = []
        self.explanations = {}
        selection = SimpleNamespace(name="active_search", query=query)
        with runtime.session_scope() as session:
            jobs = list(search_jobs(session, query))
            triage_repo = PersonalTriageRepository(session)
            decisions = triage_repo.load_decision_map([job.id for job in jobs])
            rules = triage_repo.list_rules(active_only=True)
            qualifications = {
                record.job_posting_id: record
                for record in session.scalars(select(QualificationResultRecord).where(QualificationResultRecord.job_posting_id.in_([job.id for job in jobs]))).all()
            } if jobs else {}
            for job in jobs:
                qualification = qualifications.get(job.id)
                assessment = assess_personal_job(job, qualification, [selection], runtime.config.personal, decision=decisions.get(job.id), rules=rules)
                explanation = assessment.explanation
                self.explanations[job.id] = explanation
                rows.append(
                    {
                        "id": job.id,
                        "company": job.company.display_name,
                        "title": job.title,
                        "priority": explanation.priority_label,
                        "priority_score": explanation.score,
                        "triage": explanation.triage_status.value,
                        "source": job.source_adapter,
                        "board": job.board_token or "",
                        "location": job_location(job),
                        "scope": job.location_scope or "",
                        "workplace": job.workplace_type or "",
                        "experience": job.experience_level or "",
                        "posted": job.posted_at.date().isoformat() if job.posted_at else "",
                        "posted_sort": job.posted_at.isoformat() if job.posted_at else "",
                        "comp": job_compensation(job),
                        "status": job.lifecycle_status.value,
                        "sponsorship": qualification.fit if qualification is not None else "",
                        "haystack": " ".join(
                            value.lower()
                            for value in [
                                job.company.display_name,
                                job.title,
                                job_location(job),
                                job.source_adapter,
                                job.board_token or "",
                                explanation.priority_label,
                                explanation.triage_status.value,
                            ]
                            if value
                        ),
                    }
                )
        if filter_text:
            rows = [row for row in rows if filter_text in row["haystack"]]
        if sort_key == "title":
            rows.sort(key=lambda row: (row["title"], row["company"]))
        elif sort_key == "company":
            rows.sort(key=lambda row: (row["company"], row["title"]))
        elif sort_key == "posted":
            rows.sort(key=lambda row: row["posted_sort"], reverse=True)
        elif sort_key == "priority":
            rows.sort(key=lambda row: (row["priority_score"], row["posted_sort"]), reverse=True)
        self.table.clear(columns=False)
        self.job_ids = []
        for row in rows:
            self.job_ids.append(row["id"])
            self.table.add_row(
                row["company"],
                row["title"],
                row["priority"],
                row["triage"],
                row["source"],
                row["board"],
                row["location"],
                row["scope"],
                row["workplace"],
                row["experience"],
                row["posted"],
                row["comp"],
                row["status"],
                row["sponsorship"],
            )
        if self.job_ids:
            self.show_detail(runtime, self.job_ids[0], mode="job")
        else:
            self.detail.update("No matching jobs for the current filter set.")
        return len(rows)

    def show_detail(self, runtime: AppRuntime, job_id: str, *, mode: str = "job") -> None:
        with runtime.session_scope() as session:
            job = session.get(JobPosting, job_id)
            if job is None:
                self.detail.update("Job not found.")
                return
            app_repo = ApplicationRepository(session)
            application = app_repo.get_application_for_job(job_id)
            artifacts = app_repo.list_artifacts(job_posting_id=job_id)
            qualification = session.scalar(select(QualificationResultRecord).where(QualificationResultRecord.job_posting_id == job_id))
            review_packet = next((artifact for artifact in artifacts if artifact.kind.value == "review_packet"), None)
            explanation = self.explanations.get(job_id)
            if mode == "packet":
                self.detail.update(review_packet.path if review_packet else "No review packet recorded for this job.")
                return
            if mode == "artifacts":
                self.detail.update(render_lines([f"{artifact.kind.value}: {artifact.path}" for artifact in artifacts] or ["No artifacts recorded."]))
                return
            detail_lines = [
                f"{job.company.display_name} | {job.title}",
                f"Priority: {(explanation.priority_label if explanation else '-') } | Score: {(explanation.score if explanation else '-') } | Triage: {(explanation.triage_status.value if explanation else '-')}",
                f"Source: {job.source_adapter} | Board: {job.board_token or '-'}",
                f"Location: {job_location(job)}",
                f"Location Scope: {job.location_scope or '-'} | Workplace: {job.workplace_type or '-'}",
                f"Experience: {job.experience_level or '-'} | Posted: {job.posted_at.date().isoformat() if job.posted_at else '-'}",
                f"Compensation: {job_compensation(job) or '-'}",
                f"Company Size: {job.company.company_size_bucket}",
                f"Lifecycle: {job.lifecycle_status.value}",
                f"Sponsorship Fit: {qualification.fit if qualification is not None else '-'}",
                f"Posting URL: {job.posting_url}",
                f"Apply URL: {job.apply_url or job.posting_url}",
                f"Review Packet: {review_packet.path if review_packet else '-'}",
                f"Application Status: {application.status.value if application else '-'}",
                f"Review Status: {application.review_status.value if application else '-'}",
                f"Review Flags: {', '.join(application.review_flags) if application and application.review_flags else '-'}",
            ]
            if explanation is not None:
                detail_lines.extend(
                    [
                        f"Headline: {explanation.headline or '-'}",
                        f"Matched: {', '.join(explanation.matched_query_names) or '-'}",
                        f"Why Ranked: {' | '.join(explanation.ranking_reasons[:2]) or '-'}",
                        f"Penalties: {' | '.join(explanation.penalties[:2]) or '-'}",
                        f"Warnings: {' | '.join(explanation.warnings[:2]) or '-'}",
                        f"Suppression: {' | '.join(explanation.suppression_reasons[:2]) or '-'}",
                    ]
                )
            self.detail.update(render_lines(detail_lines))
