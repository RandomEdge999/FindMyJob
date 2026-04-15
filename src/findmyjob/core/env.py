from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

_DOTENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)\s*$"
)
_MISSING: Final[object] = object()
_DOTENV_ACTIVE_WORKSPACE: Path | None = None
_DOTENV_INJECTED_VALUES: dict[str, str] = {}
_DOTENV_ORIGINAL_VALUES: dict[str, object] = {}


def load_workspace_dotenv(workspace: Path | None = None) -> Path | None:
    """Load the workspace-root `.env` into ``os.environ`` without overriding process env."""

    global _DOTENV_ACTIVE_WORKSPACE

    root = (workspace or Path.cwd()).resolve()
    dotenv_path = root / ".env"

    _restore_injected_env()
    _DOTENV_ACTIVE_WORKSPACE = root

    if not dotenv_path.is_file():
        return None

    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in os.environ:
            continue
        _DOTENV_ORIGINAL_VALUES.setdefault(key, _MISSING)
        os.environ[key] = value
        _DOTENV_INJECTED_VALUES[key] = value
    return dotenv_path


def _restore_injected_env() -> None:
    global _DOTENV_ACTIVE_WORKSPACE

    if not _DOTENV_INJECTED_VALUES and not _DOTENV_ORIGINAL_VALUES:
        return

    for key, original in list(_DOTENV_ORIGINAL_VALUES.items()):
        injected = _DOTENV_INJECTED_VALUES.get(key)
        if injected is None or os.environ.get(key) != injected:
            continue
        if original is _MISSING:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(original)

    _DOTENV_ACTIVE_WORKSPACE = None
    _DOTENV_INJECTED_VALUES.clear()
    _DOTENV_ORIGINAL_VALUES.clear()


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _DOTENV_ASSIGNMENT_RE.match(line)
    if match is None:
        return None
    key = match.group("key")
    value = _parse_dotenv_value(match.group("value") or "")
    return key, value


def _parse_dotenv_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == '"':
            return bytes(inner, "utf-8").decode("unicode_escape")
        return inner

    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
