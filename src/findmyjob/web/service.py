from __future__ import annotations

from pathlib import Path
from typing import Any

from findmyjob.filefirst.service import FileFirstOperatorService


class OperatorConsoleService(FileFirstOperatorService):
    def __init__(self, runtime_or_workspace: Any) -> None:
        workspace = getattr(runtime_or_workspace, 'workspace', runtime_or_workspace)
        super().__init__(Path(workspace))
        self.runtime = runtime_or_workspace if hasattr(runtime_or_workspace, 'workspace') else None


__all__ = ["OperatorConsoleService"]
