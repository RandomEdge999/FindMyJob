from __future__ import annotations

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Static

from findmyjob.core.runtime import AppRuntime
from findmyjob.db.board_repository import BoardRepository, SourceStateRepository
from findmyjob.db.models import AuditEventRecord
from findmyjob.tui.common import render_lines


class BoardsView(Container):
    def __init__(self) -> None:
        super().__init__(id="boards-view")
        self.table = DataTable(id="boards-table")
        self.detail = Static(id="boards-detail")
        self.board_tokens: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Board Registry", classes="screen-title")
        yield Horizontal(
            Button("Refresh", id="boards-refresh"),
            Button("Discover", id="boards-discover"),
            Button("Sync Selected", id="boards-sync-selected"),
            classes="action-row",
        )
        yield Horizontal(self.table, self.detail, classes="results-layout")

    def on_mount(self) -> None:
        self.table.add_columns("Token", "Company", "Validation", "Live", "Last Sync", "Failures", "Backoff")

    def current_board_token(self) -> str | None:
        if not self.board_tokens:
            return None
        try:
            row = self.table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.board_tokens):
            row = 0
        return self.board_tokens[row]

    def refresh_view(self, runtime: AppRuntime) -> None:
        self.table.clear(columns=False)
        self.board_tokens = []
        with runtime.session_scope() as session:
            boards = BoardRepository(session).list_boards("greenhouse", limit=50)
            for board in boards:
                state = SourceStateRepository(session).get_board_sync_state("greenhouse", board.board_token)
                self.board_tokens.append(board.board_token)
                self.table.add_row(
                    board.board_token,
                    board.company_hint or "",
                    board.validation_status,
                    str(board.live_job_count),
                    str(board.last_sync_at or ""),
                    str(board.failure_count),
                    str(state.backoff_until or ""),
                )
        if self.board_tokens:
            self.show_detail(runtime, self.board_tokens[0])
        else:
            self.detail.update("No boards recorded.")

    def show_detail(self, runtime: AppRuntime, board_token: str) -> None:
        with runtime.session_scope() as session:
            board = BoardRepository(session).get_board("greenhouse", board_token)
            if board is None:
                self.detail.update("Board not found.")
                return
            state = SourceStateRepository(session).get_board_sync_state("greenhouse", board_token)
            events = session.execute(
                select(AuditEventRecord)
                .where(AuditEventRecord.entity_type == "board_registry")
                .order_by(AuditEventRecord.created_at.desc())
                .limit(5)
            ).scalars().all()
            self.detail.update(
                render_lines(
                    [
                        f"Board: {board.board_token}",
                        f"Company: {board.company_hint or '-'}",
                        f"Validation: {board.validation_status} | Active: {board.active}",
                        f"Live Jobs: {board.live_job_count} | Failures: {board.failure_count}",
                        f"Last Sync: {board.last_sync_at or '-'} | Last Sync Status: {board.last_sync_status or '-'}",
                        f"Backoff Until: {state.backoff_until or '-'}",
                        f"Last Error: {board.last_error or '-'}",
                        f"Source URL: {board.source_url or '-'}",
                        f"Board URL: {board.board_url or '-'}",
                        "Recent Audit Events:",
                        *[f"- {event.event_type} @ {event.created_at}" for event in events],
                    ]
                )
            )
