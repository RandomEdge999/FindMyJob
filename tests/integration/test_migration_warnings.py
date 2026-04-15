from __future__ import annotations

import warnings
from pathlib import Path

from findmyjob.db.migrations import upgrade_database


def test_upgrade_database_does_not_emit_path_separator_deprecation(tmp_path: Path) -> None:
    database_path = tmp_path / 'warning-free.db'

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always', DeprecationWarning)
        upgrade_database(database_path)

    assert not [
        warning
        for warning in captured
        if isinstance(warning.message, DeprecationWarning) and 'path_separator' in str(warning.message)
    ]
