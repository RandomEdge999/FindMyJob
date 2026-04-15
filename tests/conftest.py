from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest


_TEST_TMP_ROOT = Path(
    os.environ.get(
        "FMJ_TEST_TMP_ROOT",
        str(Path.cwd() / ".tmp" / "tests" / "pytest-workspace"),
    )
)


@pytest.fixture
def tmp_path() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
