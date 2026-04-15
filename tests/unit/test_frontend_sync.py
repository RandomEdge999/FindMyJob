from __future__ import annotations

import os
from pathlib import Path

import pytest

from findmyjob.web.frontend_sync import frontend_bundle_status, sync_frontend_bundle


def _write_file(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _touch_with_mtime(path: Path, timestamp: int, content: str = "x") -> Path:
    _write_file(path, content)
    os.utime(path, (timestamp, timestamp))
    return path


def _seed_frontend_tree(tmp_path: Path, *, fresh: bool) -> tuple[Path, Path]:
    frontend_root = tmp_path / "frontend"
    dist_dir = tmp_path / "src" / "findmyjob" / "web" / "frontend_dist"
    input_time = 200 if fresh else 300
    output_time = 300 if fresh else 200

    _touch_with_mtime(frontend_root / "src" / "main.jsx", input_time, "console.log('app')\n")
    _touch_with_mtime(frontend_root / "src" / "app.jsx", input_time, "export const x = 1\n")
    _touch_with_mtime(frontend_root / "build.mjs", input_time, "build\n")
    _touch_with_mtime(frontend_root / "package.json", input_time, "{}\n")
    _touch_with_mtime(frontend_root / "package-lock.json", input_time, "{}\n")
    _touch_with_mtime(frontend_root / "vite.config.js", input_time, "export default {}\n")
    (frontend_root / "node_modules").mkdir(parents=True, exist_ok=True)

    _touch_with_mtime(dist_dir / "index.html", output_time, "<html></html>\n")
    _touch_with_mtime(dist_dir / "assets" / "index.js", output_time, "console.log('built')\n")
    _touch_with_mtime(dist_dir / "assets" / "index.css", output_time, "body{}\n")
    _touch_with_mtime(dist_dir / "assets" / "runtime-fixes.js", output_time, "window.__ok = true\n")
    return frontend_root, dist_dir


def test_frontend_sync_skips_fresh_dist(tmp_path: Path) -> None:
    _seed_frontend_tree(tmp_path, fresh=True)

    result = sync_frontend_bundle(
        tmp_path,
        run_command=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("build should not run")),
        which=lambda name: f"C:/tools/{name}.exe",
    )

    assert result.checked is True
    assert result.built is False
    assert result.reason == "fresh"


def test_frontend_sync_rebuilds_stale_dist(tmp_path: Path) -> None:
    frontend_root, dist_dir = _seed_frontend_tree(tmp_path, fresh=False)
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command, *, cwd, capture_output, text, check):
        assert capture_output is True
        assert text is True
        assert check is False
        calls.append((list(command), Path(cwd)))
        if command[-2:] == ["run", "build"]:
            fresh_time = 500
            _touch_with_mtime(dist_dir / "index.html", fresh_time, "<html>fresh</html>\n")
            _touch_with_mtime(dist_dir / "assets" / "index.js", fresh_time, "console.log('fresh')\n")
            _touch_with_mtime(dist_dir / "assets" / "index.css", fresh_time, "body{color:black}\n")
            _touch_with_mtime(dist_dir / "assets" / "runtime-fixes.js", fresh_time, "window.__fresh = true\n")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    result = sync_frontend_bundle(
        tmp_path,
        run_command=fake_run,
        which=lambda name: f"C:/tools/{name}.exe",
    )

    assert result.checked is True
    assert result.built is True
    assert result.reason == "stale_dist"
    assert calls == [(["C:/tools/npm.exe", "run", "build"], frontend_root)]
    assert frontend_bundle_status(tmp_path).needs_build is False


def test_frontend_sync_accepts_older_static_assets_after_a_recent_build(tmp_path: Path) -> None:
    frontend_root, dist_dir = _seed_frontend_tree(tmp_path, fresh=False)

    _touch_with_mtime(frontend_root / "src" / "main.jsx", 250, "console.log('app')\n")
    _touch_with_mtime(dist_dir / "index.html", 400, "<html>fresh</html>\n")
    _touch_with_mtime(dist_dir / "assets" / "index.js", 400, "console.log('fresh')\n")
    _touch_with_mtime(dist_dir / "assets" / "index.css", 150, "body{}\n")
    _touch_with_mtime(dist_dir / "assets" / "runtime-fixes.js", 150, "window.__ok = true\n")

    status = frontend_bundle_status(tmp_path)

    assert status.needs_build is False
    assert status.reason == "fresh"


def test_frontend_sync_fails_when_stale_and_node_toolchain_missing(tmp_path: Path) -> None:
    _seed_frontend_tree(tmp_path, fresh=False)

    with pytest.raises(RuntimeError, match="Node\\.js/npm are unavailable"):
        sync_frontend_bundle(
            tmp_path,
            run_command=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("build should not run")),
            which=lambda name: None,
        )


def test_frontend_sync_noops_when_frontend_sources_are_unavailable(tmp_path: Path) -> None:
    result = sync_frontend_bundle(tmp_path)

    assert result.checked is False
    assert result.built is False
    assert result.reason == "source_unavailable"
