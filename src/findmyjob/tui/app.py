from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Button, Checkbox, Footer, Header, Input, Static

from findmyjob.core.enums import ReviewStatus
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery, SavedSearch
from findmyjob.db.repositories import SavedSearchRepository
from findmyjob.orchestrator.greenhouse import GreenhouseScaleOrchestrator
from findmyjob.orchestrator.service import Orchestrator
from findmyjob.personal.workflow import archive_job, dismiss_job, shortlist_job
from findmyjob.tui.boards import BoardsView
from findmyjob.tui.dashboard import DashboardView
from findmyjob.tui.results import ResultsView
from findmyjob.tui.review import ReviewView
from findmyjob.tui.runs import RunsView
from findmyjob.tui.search_builder import SearchBuilderView
from findmyjob.tui.common import render_lines


class FindMyJobApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }
    #status-bar {
        dock: top;
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    #main {
        height: 1fr;
    }
    .hidden {
        display: none;
    }
    .screen-title {
        text-style: bold;
        margin: 1 0;
    }
    .section-title {
        text-style: bold;
        margin-top: 1;
    }
    .action-row {
        height: auto;
        margin-bottom: 1;
    }
    .builder-layout {
        height: auto;
    }
    .pane {
        width: 1fr;
        margin-right: 1;
    }
    .results-layout {
        height: 1fr;
    }
    DataTable {
        height: 18;
        width: 1fr;
    }
    #dashboard-summary, #runs-ops-summary {
        width: 1fr;
        padding: 1;
        border: round $accent;
        height: auto;
        margin-bottom: 1;
    }
    #results-detail, #boards-detail, #runs-detail, #runs-ops-detail, #review-detail {
        width: 1fr;
        padding: 1;
        border: round $accent;
        height: auto;
        min-height: 8;
    }
    #run-tasks-table, #runs-smoke-table, #runs-benchmark-table {
        height: 10;
    }
    #help-overlay, #confirm-overlay {
        dock: bottom;
        height: auto;
        padding: 1;
        background: $surface;
        border: round $warning;
    }
    Input {
        margin-bottom: 1;
    }
    Checkbox {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("1", "show_dashboard", "Dashboard"),
        ("2", "show_search_builder", "Search"),
        ("3", "show_results", "Results"),
        ("4", "show_boards", "Boards"),
        ("5", "show_runs", "Runs"),
        ("6", "show_review", "Review"),
        ("s", "show_runs_smoke", "Smoke"),
        ("b", "show_runs_benchmarks", "Bench"),
        ("r", "refresh_current", "Refresh"),
        ("?", "toggle_help", "Help"),
        ("y", "confirm_yes", "Yes"),
        ("n", "confirm_no", "No"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, workspace: Path | None = None) -> None:
        super().__init__()
        self.runtime = AppRuntime.bootstrap(workspace)
        self.status_bar = Static(id="status-bar")
        self.help_overlay = Static(id="help-overlay", classes="hidden")
        self.confirm_overlay = Static(id="confirm-overlay", classes="hidden")
        self.dashboard_view = DashboardView()
        self.search_builder_view = SearchBuilderView()
        self.results_view = ResultsView()
        self.boards_view = BoardsView()
        self.runs_view = RunsView()
        self.review_view = ReviewView()
        self.current_view = "dashboard"
        self.current_query = JobSearchQuery()
        self.pending_delete_reference: str | None = None
        self.pending_delete_name: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield self.status_bar
        yield Container(
            self.dashboard_view,
            self.search_builder_view,
            self.results_view,
            self.boards_view,
            self.runs_view,
            self.review_view,
            id="main",
        )
        yield self.help_overlay
        yield self.confirm_overlay
        yield Footer()

    def on_mount(self) -> None:
        query, saved_search = self._initial_query()
        self.current_query = query
        self.search_builder_view.load_query(query, saved_search)
        self.refresh_search_builder()
        self.refresh_dashboard()
        self.results_view.refresh_view(self.runtime, self.current_query)
        self.boards_view.refresh_view(self.runtime)
        self.runs_view.refresh_view(self.runtime)
        self.review_view.refresh_view(self.runtime)
        self.help_overlay.update(
            render_lines(
                [
                    "Operator Help",
                    "1 Dashboard | 2 Search Builder | 3 Results | 4 Boards | 5 Runs | 6 Review",
                    "s Smoke history | b Benchmark history | r Refresh current view | ? Toggle help | q Quit",
                    "Dashboard: refresh RC state, jump into smoke history, and jump into benchmark history",
                    "Search Builder: Preview, Save, Load, Delete, Set Default, Sync Greenhouse",
                    "Results: filter, inspect selected job, show packet/artifacts, prepare selected job",
                    "Boards: discover boards, inspect registry, sync selected board",
                    "Runs: inspect checkpoints plus smoke and benchmark history",
                    "Review: approve, reject, request input, or handoff queued applications",
                ]
            )
        )
        self.switch_view("dashboard")
        self.set_status(f"Operator console ready. Current search: {self.current_query.summary()}")

    def _initial_query(self) -> tuple[JobSearchQuery, SavedSearch | None]:
        with self.runtime.session_scope() as session:
            repo = SavedSearchRepository(session)
            default_record = repo.get_default()
            if default_record is not None:
                saved_search = repo.to_model(default_record)
                return saved_search.query_payload.model_copy(deep=True), saved_search
        return JobSearchQuery.from_search_settings(self.runtime.config.search), None

    def _views(self):
        return {
            "dashboard": self.dashboard_view,
            "search": self.search_builder_view,
            "results": self.results_view,
            "boards": self.boards_view,
            "runs": self.runs_view,
            "review": self.review_view,
        }

    def set_status(self, message: str) -> None:
        self.status_bar.update(message)

    def switch_view(self, name: str) -> None:
        for view_name, view in self._views().items():
            if view_name == name:
                view.remove_class("hidden")
            else:
                view.add_class("hidden")
        self.current_view = name

    def refresh_search_builder(self) -> None:
        with self.runtime.session_scope() as session:
            searches = SavedSearchRepository(session).list_models()
        self.search_builder_view.refresh_saved_searches(searches)
        self.search_builder_view.update_summary()

    def refresh_dashboard(self) -> None:
        self.dashboard_view.refresh_view(self.runtime, self.current_query.summary())
        self.set_status("Dashboard release state refreshed")

    def refresh_results(self) -> None:
        count = self.results_view.refresh_view(self.runtime, self.current_query)
        self.set_status(f"Results refreshed: {count} jobs for {self.current_query.summary()}")

    def refresh_boards(self) -> None:
        self.boards_view.refresh_view(self.runtime)
        self.set_status("Boards refreshed")

    def refresh_runs(self) -> None:
        self.runs_view.refresh_view(self.runtime)
        self.set_status("Runs and release operations refreshed")

    def refresh_review(self) -> None:
        self.review_view.refresh_view(self.runtime)
        self.set_status("Review queue refreshed")

    async def action_show_dashboard(self) -> None:
        self.switch_view("dashboard")
        self.refresh_dashboard()

    async def action_show_search_builder(self) -> None:
        self.switch_view("search")
        self.refresh_search_builder()

    async def action_show_results(self) -> None:
        self.switch_view("results")
        self.refresh_results()

    async def action_show_boards(self) -> None:
        self.switch_view("boards")
        self.refresh_boards()

    async def action_show_runs(self) -> None:
        self.switch_view("runs")
        self.refresh_runs()

    async def action_show_runs_smoke(self) -> None:
        self.switch_view("runs")
        self.refresh_runs()
        self.runs_view.focus_smoke_history()
        self.set_status("Showing recent smoke history")

    async def action_show_runs_benchmarks(self) -> None:
        self.switch_view("runs")
        self.refresh_runs()
        self.runs_view.focus_benchmark_history()
        self.set_status("Showing recent benchmark history")

    async def action_show_review(self) -> None:
        self.switch_view("review")
        self.refresh_review()

    async def action_refresh_current(self) -> None:
        if self.current_view == "dashboard":
            self.refresh_dashboard()
        elif self.current_view == "search":
            self.refresh_search_builder()
        elif self.current_view == "results":
            self.refresh_results()
        elif self.current_view == "boards":
            self.refresh_boards()
        elif self.current_view == "runs":
            self.refresh_runs()
        elif self.current_view == "review":
            self.refresh_review()

    async def action_toggle_help(self) -> None:
        if self.help_overlay.has_class("hidden"):
            self.help_overlay.remove_class("hidden")
        else:
            self.help_overlay.add_class("hidden")

    async def action_confirm_yes(self) -> None:
        if not self.pending_delete_reference:
            return
        with self.runtime.session_scope() as session:
            repo = SavedSearchRepository(session)
            repo.delete(self.pending_delete_reference)
        deleted_name = self.pending_delete_name or self.pending_delete_reference
        self.pending_delete_reference = None
        self.pending_delete_name = None
        self.confirm_overlay.add_class("hidden")
        self.refresh_search_builder()
        self.set_status(f"Deleted saved search: {deleted_name}")

    async def action_confirm_no(self) -> None:
        self.pending_delete_reference = None
        self.pending_delete_name = None
        self.confirm_overlay.add_class("hidden")

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("search-"):
            self.search_builder_view.update_summary()
        if event.input.id in {"results-filter", "results-sort"} and self.current_view == "results":
            self.refresh_results()

    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("search-"):
            self.search_builder_view.update_summary()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "preview-results":
            self.current_query = self.search_builder_view.build_query()
            self.switch_view("results")
            self.refresh_results()
        elif button_id == "save-search":
            await self._save_current_search()
        elif button_id == "load-search":
            await self._load_selected_search()
        elif button_id == "delete-search":
            self._confirm_delete_selected_search()
        elif button_id == "set-default-search":
            await self._set_selected_search_default()
        elif button_id == "sync-greenhouse":
            await self._run_greenhouse_sync()
        elif button_id == "reset-search":
            self.search_builder_view.reset_form()
            self.current_query = JobSearchQuery()
            self.set_status("Builder reset")
        elif button_id == "results-refresh":
            self.current_query = self.search_builder_view.build_query()
            self.refresh_results()
        elif button_id == "results-inspect":
            job_id = self.results_view.current_job_id()
            if job_id:
                self.results_view.show_detail(self.runtime, job_id, mode="job")
        elif button_id == "results-shortlist":
            await self._triage_selected_job("shortlist")
        elif button_id == "results-dismiss":
            await self._triage_selected_job("dismiss")
        elif button_id == "results-archive":
            await self._triage_selected_job("archive")
        elif button_id == "results-packet":
            job_id = self.results_view.current_job_id()
            if job_id:
                self.results_view.show_detail(self.runtime, job_id, mode="packet")
                self.set_status("Showing review packet path for selected job")
        elif button_id == "results-artifacts":
            job_id = self.results_view.current_job_id()
            if job_id:
                self.results_view.show_detail(self.runtime, job_id, mode="artifacts")
                self.set_status("Showing recorded artifacts for selected job")
        elif button_id == "results-prepare":
            await self._prepare_selected_job()
        elif button_id == "boards-refresh":
            self.refresh_boards()
        elif button_id == "dashboard-refresh-rc":
            self.refresh_dashboard()
        elif button_id == "dashboard-open-smoke":
            await self.action_show_runs_smoke()
        elif button_id == "dashboard-open-benchmarks":
            await self.action_show_runs_benchmarks()
        elif button_id == "boards-discover":
            await GreenhouseScaleOrchestrator(self.runtime).discover_boards()
            self.refresh_dashboard()
            self.refresh_boards()
            self.refresh_runs()
            self.set_status("Greenhouse board discovery completed")
        elif button_id == "boards-sync-selected":
            await self._sync_selected_board()
        elif button_id == "runs-refresh":
            self.refresh_runs()
        elif button_id == "runs-inspect":
            run_id = self.runs_view.current_run_id()
            if run_id:
                self.runs_view.show_detail(self.runtime, run_id)
                self.set_status(f"Showing run detail: {run_id[:12]}")
        elif button_id == "runs-inspect-smoke":
            self.runs_view.show_current_smoke_detail()
            self.set_status("Showing smoke result detail")
        elif button_id == "runs-inspect-benchmark":
            self.runs_view.show_current_benchmark_detail()
            self.set_status("Showing benchmark detail")
        elif button_id == "review-refresh":
            self.refresh_review()
        elif button_id == "review-approve":
            await self._review_selected(ReviewStatus.APPROVED)
        elif button_id == "review-reject":
            await self._review_selected(ReviewStatus.REJECTED)
        elif button_id == "review-request-input":
            await self._review_selected(ReviewStatus.NEEDS_USER_INPUT)
        elif button_id == "review-handoff":
            await self._review_selected(ReviewStatus.MANUAL_HANDOFF)

    async def _save_current_search(self) -> None:
        name = self.search_builder_view.name_input.value.strip()
        if not name:
            self.set_status("Saved search name is required")
            return
        query = self.search_builder_view.build_query()
        saved = SavedSearch(
            name=name,
            description=self.search_builder_view.description_input.value.strip() or None,
            query_payload=query,
            source_adapter_hint=query.source_adapter,
            is_default=self.search_builder_view.default_on_save.value,
        )
        with self.runtime.session_scope() as session:
            repo = SavedSearchRepository(session)
            record = repo.save(saved)
            stored = repo.to_model(record)
        self.refresh_search_builder()
        self.search_builder_view.load_query(query, stored)
        self.current_query = query
        self.set_status(f"Saved search: {stored.name}")

    async def _load_selected_search(self) -> None:
        reference = self.search_builder_view.current_saved_search_reference()
        if not reference:
            self.set_status("No saved search selected")
            return
        with self.runtime.session_scope() as session:
            repo = SavedSearchRepository(session)
            record = repo.require(reference)
            repo.touch_last_used(record.id)
            saved = repo.to_model(record)
        self.current_query = saved.query_payload.model_copy(deep=True)
        self.search_builder_view.load_query(self.current_query, saved)
        self.refresh_search_builder()
        self.set_status(f"Loaded saved search: {saved.name}")

    def _confirm_delete_selected_search(self) -> None:
        reference = self.search_builder_view.current_saved_search_reference()
        if not reference:
            self.set_status("No saved search selected")
            return
        with self.runtime.session_scope() as session:
            repo = SavedSearchRepository(session)
            saved = repo.to_model(repo.require(reference))
        self.pending_delete_reference = reference
        self.pending_delete_name = saved.name
        self.confirm_overlay.update(f"Delete saved search '{saved.name}'? Press y to confirm or n to cancel.")
        self.confirm_overlay.remove_class("hidden")

    async def _set_selected_search_default(self) -> None:
        reference = self.search_builder_view.current_saved_search_reference()
        if not reference:
            self.set_status("No saved search selected")
            return
        with self.runtime.session_scope() as session:
            repo = SavedSearchRepository(session)
            record = repo.mark_default(reference)
            saved = repo.to_model(record)
        self.refresh_search_builder()
        self.set_status(f"Default saved search set to: {saved.name}")

    async def _run_greenhouse_sync(self) -> None:
        query = self.search_builder_view.build_query()
        if query.source_adapter and query.source_adapter != "greenhouse":
            self.set_status("Greenhouse sync only accepts greenhouse or empty source filters")
            return
        await GreenhouseScaleOrchestrator(self.runtime).sync_boards(query_override=query)
        self.current_query = query
        self.refresh_dashboard()
        self.refresh_boards()
        self.refresh_results()
        self.refresh_runs()
        self.set_status("Greenhouse sync completed for current search profile")

    async def _prepare_selected_job(self) -> None:
        job_id = self.results_view.current_job_id()
        if not job_id:
            self.set_status("No job selected")
            return
        await Orchestrator(self.runtime).run_prepare_for_job(job_id)
        self.refresh_dashboard()
        self.refresh_results()
        self.refresh_review()
        self.refresh_runs()
        self.set_status(f"Prepared job for review: {job_id}")

    async def _triage_selected_job(self, action: str) -> None:
        job_id = self.results_view.current_job_id()
        if not job_id:
            self.set_status("No job selected")
            return
        if action == "shortlist":
            await shortlist_job(self.runtime, job_id, "tui_shortlist", "Results view shortlist")
        elif action == "dismiss":
            await dismiss_job(self.runtime, job_id, "tui_dismiss", "Results view dismiss")
        elif action == "archive":
            await archive_job(self.runtime, job_id, "tui_archive", "Results view archive")
        else:
            self.set_status(f"Unknown triage action: {action}")
            return
        self.refresh_dashboard()
        self.refresh_results()
        self.set_status(f"Updated triage for {job_id}: {action}")

    async def _sync_selected_board(self) -> None:
        board_token = self.boards_view.current_board_token()
        if not board_token:
            self.set_status("No board selected")
            return
        query = self.current_query.model_copy(deep=True)
        query.source_adapter = "greenhouse"
        query.board_token = board_token
        await GreenhouseScaleOrchestrator(self.runtime).sync_boards(query_override=query)
        self.refresh_dashboard()
        self.refresh_boards()
        self.refresh_runs()
        self.set_status(f"Synced selected board: {board_token}")

    async def _review_selected(self, action: ReviewStatus) -> None:
        application_id = self.review_view.current_application_id()
        if not application_id:
            self.set_status("No application selected")
            return
        reason = "Manual handoff from TUI" if action == ReviewStatus.MANUAL_HANDOFF else None
        await Orchestrator(self.runtime).review_action(application_id, action, reason)
        self.refresh_dashboard()
        self.refresh_review()
        self.refresh_results()
        self.refresh_runs()
        self.set_status(f"Updated review status for {application_id}: {action.value}")





