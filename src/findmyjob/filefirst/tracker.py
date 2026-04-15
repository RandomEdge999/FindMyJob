from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Footer, Header, Static

from findmyjob.filefirst.models import ApplicationEntry
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.tui.common import render_lines

FILTER_ORDER = ["all", "evaluated", "pdf_ready", "preview_ready", "applied", "archived"]
FILTER_LABELS = {
    "all": "All",
    "evaluated": "Evaluated",
    "pdf_ready": "PDF Ready",
    "preview_ready": "Preview Ready",
    "applied": "Applied",
    "archived": "Archived",
}
SORT_ORDER = ["date", "score", "company"]
SORT_LABELS = {"date": "Date", "score": "Score", "company": "Company"}
WORKFLOW_STATES = {
    "Evaluated": "evaluated",
    "PDF Ready": "pdf_ready",
    "Applied": "applied",
    "Archived": "archived",
}


def _status_bucket(entry: ApplicationEntry) -> str:
    status = entry.status.casefold()
    if "archiv" in status:
        return "archived"
    if "preview" in status:
        return "preview_ready"
    if "appl" in status or "submit" in status:
        return "applied"
    if entry.pdf or "pdf" in status:
        return "pdf_ready"
    return "evaluated"



def tracker_snapshot(workspace: Path | FileWorkspace) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    entries = ws.load_applications()
    counts = Counter(_status_bucket(entry) for entry in entries)
    return {
        "workspace": str(ws.root),
        "total": len(entries),
        "counts": {name: counts.get(name, 0) for name in FILTER_ORDER if name != "all"},
        "applications": [entry.model_dump(mode="json") for entry in entries],
    }


class FileFirstTrackerApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }
    #tracker-status {
        dock: top;
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    .tracker-row {
        height: auto;
        margin: 0 0 1 0;
    }
    #tracker-layout {
        height: 1fr;
    }
    DataTable {
        width: 3fr;
        height: 1fr;
    }
    #tracker-preview {
        width: 2fr;
        padding: 1;
        border: round $accent;
        min-height: 12;
    }
    """

    BINDINGS = [
        ("1", "filter_all", "All"),
        ("2", "filter_evaluated", "Evaluated"),
        ("3", "filter_pdf_ready", "PDF Ready"),
        ("4", "filter_applied", "Applied"),
        ("5", "filter_archived", "Archived"),
        ("r", "refresh_tracker", "Refresh"),
        ("s", "cycle_sort", "Sort"),
        ("g", "toggle_grouped", "Grouped"),
        ("e", "mark_evaluated", "Evaluated"),
        ("p", "mark_pdf_ready", "PDF Ready"),
        ("a", "mark_applied", "Applied"),
        ("x", "mark_archived", "Archived"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, workspace: Path | FileWorkspace) -> None:
        super().__init__()
        self.workspace = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
        self.status_bar = Static(id="tracker-status")
        self.table = DataTable(id="tracker-table")
        self.preview = Static(id="tracker-preview")
        self.filter_mode = "all"
        self.sort_mode = "date"
        self.grouped = False
        self.row_targets: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield self.status_bar
        yield Horizontal(
            Button("All", id="tracker-filter-all"),
            Button("Evaluated", id="tracker-filter-evaluated"),
            Button("PDF Ready", id="tracker-filter-pdf_ready"),
            Button("Applied", id="tracker-filter-applied"),
            Button("Archived", id="tracker-filter-archived"),
            classes="tracker-row",
        )
        yield Horizontal(
            Button("Sort", id="tracker-sort"),
            Button("Group", id="tracker-group"),
            Button("Set Evaluated", id="tracker-status-evaluated"),
            Button("Set PDF Ready", id="tracker-status-pdf_ready"),
            Button("Set Applied", id="tracker-status-applied"),
            Button("Set Archived", id="tracker-status-archived"),
            classes="tracker-row",
        )
        yield Horizontal(self.table, self.preview, id="tracker-layout")
        yield Footer()

    def on_mount(self) -> None:
        self.workspace.ensure()
        self.table.cursor_type = "row"
        self.table.add_columns("ID", "Date", "Company", "Role", "Score", "Grade", "Status", "PDF")
        self.refresh_view()

    def _sorted_entries(self, entries: list[ApplicationEntry]) -> list[ApplicationEntry]:
        if self.sort_mode == "score":
            return sorted(entries, key=lambda item: (item.score, item.date, item.id), reverse=True)
        if self.sort_mode == "company":
            return sorted(entries, key=lambda item: (item.company.casefold(), item.role.casefold(), item.date), reverse=False)
        return sorted(entries, key=lambda item: (item.date, item.id), reverse=True)

    def _visible_entries(self) -> list[ApplicationEntry]:
        entries = self.workspace.load_applications()
        if self.filter_mode != "all":
            entries = [entry for entry in entries if _status_bucket(entry) == self.filter_mode]
        return self._sorted_entries(entries)

    def refresh_view(self) -> None:
        entries = self._visible_entries()
        counts = Counter(_status_bucket(entry) for entry in self.workspace.load_applications())
        self.table.clear(columns=False)
        self.row_targets = []

        if self.grouped:
            grouped: dict[str, list[ApplicationEntry]] = {name: [] for name in FILTER_ORDER if name != "all"}
            for entry in entries:
                grouped.setdefault(_status_bucket(entry), []).append(entry)
            for bucket in FILTER_ORDER:
                if bucket == "all":
                    continue
                current = grouped.get(bucket) or []
                if not current:
                    continue
                self.table.add_row(FILTER_LABELS[bucket], "", "", "", "", "", "", "")
                self.row_targets.append(None)
                for entry in current:
                    self._append_entry(entry)
        else:
            for entry in entries:
                self._append_entry(entry)

        self.status_bar.update(
            f"Tracker | Filter: {FILTER_LABELS[self.filter_mode]} | Sort: {SORT_LABELS[self.sort_mode]} | "
            f"Grouped: {'on' if self.grouped else 'off'} | Evaluated: {counts.get('evaluated', 0)} | "
            f"PDF Ready: {counts.get('pdf_ready', 0)} | Preview Ready: {counts.get('preview_ready', 0)} | Applied: {counts.get('applied', 0)} | Archived: {counts.get('archived', 0)}"
        )

        current_id = self.current_application_id()
        if current_id is not None:
            self.show_detail(current_id)
        elif entries:
            self.show_detail(entries[0].id)
        else:
            self.preview.update("No applications in the tracker for the current filter.")

    def _append_entry(self, entry: ApplicationEntry) -> None:
        self.table.add_row(
            entry.id,
            entry.date,
            entry.company,
            entry.role,
            f"{entry.score:.2f}",
            entry.grade,
            entry.status,
            "yes" if entry.pdf else "no",
        )
        self.row_targets.append(entry.id)

    def current_application_id(self) -> str | None:
        if not self.row_targets:
            return None
        try:
            row = self.table.cursor_row
        except Exception:
            row = 0
        row = max(0, min(row, len(self.row_targets) - 1))
        if self.row_targets[row] is not None:
            return self.row_targets[row]
        for candidate in range(row + 1, len(self.row_targets)):
            if self.row_targets[candidate] is not None:
                return self.row_targets[candidate]
        for candidate in range(row - 1, -1, -1):
            if self.row_targets[candidate] is not None:
                return self.row_targets[candidate]
        return None

    def show_detail(self, application_id: str) -> None:
        entry = self.workspace.find_application(application_id)
        if entry is None:
            self.preview.update("Application not found.")
            return
        job = self.workspace.load_job(entry.job_id)
        evaluation = self.workspace.load_evaluation(entry.job_id)
        report_path = (self.workspace.root / entry.report).resolve() if entry.report else None
        report_excerpt = None
        if report_path is not None and report_path.exists():
            report_lines = [line.rstrip() for line in report_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            report_excerpt = "\n".join(report_lines[:12])
        preview_lines = [
            f"{entry.company} | {entry.role}",
            f"Application: {entry.id}",
            f"Status: {entry.status} | Score: {entry.score:.2f} ({entry.grade})",
            f"PDF: {'yes' if entry.pdf else 'no'}",
            f"URL: {entry.url}",
        ]
        if job is not None:
            preview_lines.extend(
                [
                    f"Source: {job.source} | Board: {job.board_family} | Tier: {job.automation_tier}",
                    f"ATS: {job.ats_family} | Previewable: {'yes' if job.ats_preview_supported else 'no'} | Eligible: {'yes' if job.rehearsal_eligible else 'no'}",
                    f"Location: {job.location or '-'}",
                ]
            )
            if job.hard_reject_reason:
                preview_lines.append(f"Hard Reject: {job.hard_reject_reason}")
            if job.auth_reject_reason:
                preview_lines.append(f"Auth Reject: {job.auth_reject_reason}")
            if job.login_wall_detected:
                preview_lines.append('Login wall detected on apply surface')
            if job.screening is not None:
                preview_lines.extend(
                    [
                        f"Screening: {job.screening.status} | Confidence: {job.screening.confidence:.2f}",
                        *(f"- {item}" for item in job.screening.reasons[:3]),
                    ]
                )
        if evaluation is not None:
            preview_lines.extend(
                [
                    "",
                    "Summary",
                    evaluation.summary or "No summary.",
                    "",
                    "Fit Reasons",
                    *(f"- {item}" for item in evaluation.fit_reasons[:5]),
                    "",
                    "Gaps",
                    *(f"- {item}" for item in evaluation.gaps[:5]),
                ]
            )
        if report_excerpt:
            preview_lines.extend(["", "Report", report_excerpt])
        self.preview.update(render_lines(preview_lines))

    def _set_filter(self, filter_mode: str) -> None:
        self.filter_mode = filter_mode
        self.refresh_view()

    def _cycle_sort(self) -> None:
        index = SORT_ORDER.index(self.sort_mode)
        self.sort_mode = SORT_ORDER[(index + 1) % len(SORT_ORDER)]
        self.refresh_view()

    def _update_status(self, new_status: str) -> None:
        application_id = self.current_application_id()
        if application_id is None:
            self.status_bar.update("Tracker | No application selected.")
            return
        entry = self.workspace.find_application(application_id)
        if entry is None:
            self.status_bar.update("Tracker | Selected application disappeared from disk.")
            return
        updated = entry.model_copy(update={"status": new_status, "pdf": entry.pdf or new_status == "PDF Ready"})
        self.workspace.upsert_application(updated)
        workflow_state = WORKFLOW_STATES.get(new_status)
        if workflow_state:
            self.workspace.update_inbox_state(updated.job_id, workflow_state)
        self.refresh_view()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("tracker-filter-"):
            self._set_filter(button_id.removeprefix("tracker-filter-"))
            return
        if button_id == "tracker-sort":
            self._cycle_sort()
            return
        if button_id == "tracker-group":
            self.action_toggle_grouped()
            return
        if button_id.startswith("tracker-status-"):
            mapping = {
                "evaluated": "Evaluated",
                "pdf_ready": "PDF Ready",
                "applied": "Applied",
                "archived": "Archived",
            }
            self._update_status(mapping[button_id.removeprefix("tracker-status-")])

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row < 0 or event.cursor_row >= len(self.row_targets):
            return
        application_id = self.row_targets[event.cursor_row]
        if application_id is not None:
            self.show_detail(application_id)

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_evaluated(self) -> None:
        self._set_filter("evaluated")

    def action_filter_pdf_ready(self) -> None:
        self._set_filter("pdf_ready")

    def action_filter_applied(self) -> None:
        self._set_filter("applied")

    def action_filter_archived(self) -> None:
        self._set_filter("archived")

    def action_refresh_tracker(self) -> None:
        self.refresh_view()

    def action_cycle_sort(self) -> None:
        self._cycle_sort()

    def action_toggle_grouped(self) -> None:
        self.grouped = not self.grouped
        self.refresh_view()

    def action_mark_evaluated(self) -> None:
        self._update_status("Evaluated")

    def action_mark_pdf_ready(self) -> None:
        self._update_status("PDF Ready")

    def action_mark_applied(self) -> None:
        self._update_status("Applied")

    def action_mark_archived(self) -> None:
        self._update_status("Archived")



def launch_tracker(workspace: Path | FileWorkspace) -> None:
    app = FileFirstTrackerApp(workspace)
    app.run()


__all__ = ["FileFirstTrackerApp", "launch_tracker", "tracker_snapshot"]
