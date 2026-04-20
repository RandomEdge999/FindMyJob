from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys


def _module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("verify_release", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reset_dir_uses_requested_path_when_clean(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / ".tmp" / "release-venv"

    result = module.reset_dir(target, project_root=tmp_path)

    assert result == target.resolve()
    assert result.exists()


def test_reset_dir_falls_back_to_fresh_path_when_remove_fails(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    target = tmp_path / ".tmp" / "release-venv"
    target.mkdir(parents=True, exist_ok=True)
    (target / "locked.txt").write_text("locked", encoding="utf-8")

    def fake_rmtree(path: Path) -> None:
        raise PermissionError("locked by another process")

    monkeypatch.setattr(module.shutil, "rmtree", fake_rmtree)

    result = module.reset_dir(target, project_root=tmp_path)

    assert result != target.resolve()
    assert result.parent == target.resolve().parent
    assert result.name.startswith("release-venv.fresh-")
    assert result.exists()


def test_subprocess_env_strips_python_path_overrides(monkeypatch) -> None:
    module = _module()
    monkeypatch.setenv("PYTHONPATH", "C:/bad/path")
    monkeypatch.setenv("PYTHONHOME", "C:/bad/home")
    monkeypatch.setenv("PYTHONSTARTUP", "C:/bad/startup.py")
    monkeypatch.setenv("PYTHONUSERBASE", "C:/bad/userbase")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "C:/bad/launcher.exe")

    env = module.subprocess_env()

    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert "PYTHONSTARTUP" not in env
    assert "PYTHONUSERBASE" not in env
    assert "__PYVENV_LAUNCHER__" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_module_command_uses_python_module_entrypoint(tmp_path: Path) -> None:
    module = _module()
    python_exe = tmp_path / "Scripts" / "python.exe"

    command = module.module_command(python_exe, "version", "--json")

    assert command == [str(python_exe), "-m", "findmyjob", "version", "--json"]


def test_main_allocates_unique_temp_paths_when_not_explicitly_provided(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _module()
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = dist_dir / "findmyjob-0.1.0-py3-none-any.whl"
    sdist_path = dist_dir / "findmyjob-0.1.0.tar.gz"
    wheel_path.write_text("wheel", encoding="utf-8")
    sdist_path.write_text("sdist", encoding="utf-8")

    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: SimpleNamespace(
            project_root=tmp_path,
            dist_dir=Path("dist"),
            workspace_dir=None,
            venv_dir=None,
            skip_build=True,
            ignore_requires_python=False,
        ),
    )

    monkeypatch.setattr(module.time, "time", lambda: 1234.5)
    monkeypatch.setattr(module.os, "getpid", lambda: 4321)

    created_env_dirs: list[Path] = []

    class FakeEnvBuilder:
        def __init__(self, **_kwargs) -> None:
            pass

        def create(self, env_dir: Path) -> None:
            created_env_dirs.append(Path(env_dir))

    class Completed:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout

    commands: list[tuple[list[str], Path | None]] = []
    json_commands: list[tuple[list[str], Path | None]] = []

    expected_workspace = tmp_path / ".tmp" / "release-workspace.fresh-4321-1234500-1"
    expected_venv = tmp_path / ".tmp" / "release-venv.fresh-4321-1234500-1"
    expected_python = expected_venv / "Scripts" / "python.exe"
    expected_console = expected_venv / "Scripts" / "fmj.exe"

    def fake_run(command: list[str], *, cwd=None, expected_codes=(0,)):
        _ = expected_codes
        commands.append((command, cwd))
        if command == [str(expected_python), "-c", "import findmyjob; print(findmyjob.__version__)"]:
            return Completed("0.1.0\n")
        if command == [str(expected_python), "-m", "findmyjob", "init", "--workspace", str(expected_workspace)]:
            config_path = expected_workspace / ".fmj" / "config.toml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                'default_application_mode = "dry_run"\nrequire_human_review_for_submit = true\nsubmit_enabled = false\nsubmit_enabled = false\nsubmit_enabled = false\n',
                encoding="utf-8",
            )
            template_dir = expected_workspace / ".fmj" / "local-overrides" / "filefirst"
            template_dir.mkdir(parents=True, exist_ok=True)
            (template_dir / "user-profile.template.yml").write_text("candidate: {}\n", encoding="utf-8")
            typst_dir = expected_workspace / "templates" / "typst"
            typst_dir.mkdir(parents=True, exist_ok=True)
            (typst_dir / "resume.typ").write_text("resume", encoding="utf-8")
            (typst_dir / "cover_letter.typ").write_text("cover", encoding="utf-8")
        return Completed()

    def fake_json_command(command: list[str], *, cwd=None, expected_codes=(0,)):
        _ = expected_codes
        json_commands.append((command, cwd))
        if command == [str(expected_python), "-m", "findmyjob", "version", "--json"]:
            return {"version": "0.1.0"}
        if command == [str(expected_console), "version", "--json"]:
            return {"version": "0.1.0"}
        if command[0] == str(expected_python) and command[1] == "-c":
            return {"frontend_index_exists": True}
        if command == [str(expected_python), "-m", "findmyjob", "db", "current", "--json", "--workspace", str(expected_workspace)]:
            return {"revision": "0009_answer_confidence"}
        if command == [str(expected_python), "-m", "findmyjob", "config", "validate", "--json", "--workspace", str(expected_workspace)]:
            return {"overall_status": "warnings"}
        if command == [str(expected_python), "-m", "findmyjob", "greenhouse", "smoke-results", "--json", "--workspace", str(expected_workspace)]:
            return []
        if command == [str(expected_python), "-m", "findmyjob", "greenhouse", "benchmark-results", "--json", "--workspace", str(expected_workspace)]:
            return []
        if command == [str(expected_python), "-m", "findmyjob", "doctor", "--json", "--workspace", str(expected_workspace)]:
            return {"report": {"overall_status": "blocked"}}
        if command == [str(expected_python), "-m", "findmyjob", "launch-check", "--json", "--workspace", str(expected_workspace)]:
            return {"overall_status": "fail"}
        raise AssertionError(f"Unexpected JSON command: {command}")

    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module, "latest_artifact", lambda dist, pattern: wheel_path if pattern.endswith(".whl") else sdist_path)
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "json_command", fake_json_command)
    monkeypatch.setattr(module, "console_script_path", lambda _venv_dir: expected_console)

    result = module.main()
    output = capsys.readouterr().out

    assert result == 0
    assert created_env_dirs == [expected_venv]
    assert ([str(expected_python), "-m", "pip", "install", str(wheel_path)], expected_venv) in commands
    assert ([str(expected_python), "-m", "findmyjob", "init", "--workspace", str(expected_workspace)], expected_workspace) in commands
    assert ([str(expected_console), "version", "--json"], expected_venv) in json_commands
    assert "Clean sample launch-check failure is expected" in output


def test_main_uses_reset_dir_results_for_venv_and_workspace(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = dist_dir / "findmyjob-0.1.0-py3-none-any.whl"
    sdist_path = dist_dir / "findmyjob-0.1.0.tar.gz"
    wheel_path.write_text("wheel", encoding="utf-8")
    sdist_path.write_text("sdist", encoding="utf-8")

    original_workspace = tmp_path / ".tmp" / "release-workspace"
    original_venv = tmp_path / ".tmp" / "release-venv"
    fallback_workspace = tmp_path / ".tmp" / "release-workspace.fresh-1"
    fallback_venv = tmp_path / ".tmp" / "release-venv.fresh-1"
    fallback_venv_python = fallback_venv / "Scripts" / "python.exe"
    fallback_console = fallback_venv / "Scripts" / "fmj.exe"

    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: SimpleNamespace(
            project_root=tmp_path,
            dist_dir=Path("dist"),
            workspace_dir=Path(".tmp/release-workspace"),
            venv_dir=Path(".tmp/release-venv"),
            skip_build=True,
            ignore_requires_python=False,
        ),
    )

    def fake_reset_dir(path: Path, *, project_root: Path) -> Path:
        resolved = path.resolve()
        if resolved == original_venv.resolve():
            fallback_venv.mkdir(parents=True, exist_ok=True)
            return fallback_venv
        if resolved == original_workspace.resolve():
            fallback_workspace.mkdir(parents=True, exist_ok=True)
            return fallback_workspace
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    created_env_dirs: list[Path] = []

    class FakeEnvBuilder:
        def __init__(self, **_kwargs) -> None:
            pass

        def create(self, env_dir: Path) -> None:
            created_env_dirs.append(Path(env_dir))

    commands: list[tuple[list[str], Path | None]] = []

    class Completed:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout

    def fake_run(command: list[str], *, cwd=None, expected_codes=(0,)):
        _ = (cwd, expected_codes)
        commands.append((command, cwd))
        if command == [str(fallback_venv_python), "-c", "import findmyjob; print(findmyjob.__version__)"]:
            return Completed("0.1.0\n")
        if command == [str(fallback_venv_python), "-m", "findmyjob", "init", "--workspace", str(fallback_workspace)]:
            config_path = fallback_workspace / ".fmj" / "config.toml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                'default_application_mode = "dry_run"\nrequire_human_review_for_submit = true\nsubmit_enabled = false\nsubmit_enabled = false\nsubmit_enabled = false\n',
                encoding="utf-8",
            )
            template_dir = fallback_workspace / ".fmj" / "local-overrides" / "filefirst"
            template_dir.mkdir(parents=True, exist_ok=True)
            (template_dir / "user-profile.template.yml").write_text("candidate: {}\n", encoding="utf-8")
            typst_dir = fallback_workspace / "templates" / "typst"
            typst_dir.mkdir(parents=True, exist_ok=True)
            (typst_dir / "resume.typ").write_text("resume", encoding="utf-8")
            (typst_dir / "cover_letter.typ").write_text("cover", encoding="utf-8")
        return Completed()

    json_commands: list[tuple[list[str], Path | None]] = []

    def fake_json_command(command: list[str], *, cwd=None, expected_codes=(0,)):
        _ = expected_codes
        json_commands.append((command, cwd))
        if command == [str(fallback_venv_python), "-m", "findmyjob", "version", "--json"]:
            return {"version": "0.1.0"}
        if command == [str(fallback_console), "version", "--json"]:
            return {"version": "0.1.0"}
        if command[0] == str(fallback_venv_python) and command[1] == "-c":
            return {"frontend_index_exists": True}
        if command == [str(fallback_venv_python), "-m", "findmyjob", "db", "current", "--json", "--workspace", str(fallback_workspace)]:
            return {"revision": "0009_answer_confidence"}
        if command == [str(fallback_venv_python), "-m", "findmyjob", "config", "validate", "--json", "--workspace", str(fallback_workspace)]:
            return {"overall_status": "warnings"}
        if command == [str(fallback_venv_python), "-m", "findmyjob", "greenhouse", "smoke-results", "--json", "--workspace", str(fallback_workspace)]:
            return []
        if command == [str(fallback_venv_python), "-m", "findmyjob", "greenhouse", "benchmark-results", "--json", "--workspace", str(fallback_workspace)]:
            return []
        if command == [str(fallback_venv_python), "-m", "findmyjob", "doctor", "--json", "--workspace", str(fallback_workspace)]:
            return {"report": {"overall_status": "blocked"}}
        if command == [str(fallback_venv_python), "-m", "findmyjob", "launch-check", "--json", "--workspace", str(fallback_workspace)]:
            return {"overall_status": "fail"}
        raise AssertionError(f"Unexpected JSON command: {command}")

    monkeypatch.setattr(module, "reset_dir", fake_reset_dir)
    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module, "latest_artifact", lambda dist, pattern: wheel_path if pattern.endswith(".whl") else sdist_path)
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "json_command", fake_json_command)
    monkeypatch.setattr(module, "console_script_path", lambda _venv_dir: fallback_console)

    result = module.main()

    assert result == 0
    assert created_env_dirs == [fallback_venv]
    assert ([str(fallback_venv_python), "-m", "findmyjob", "init", "--workspace", str(fallback_workspace)], fallback_workspace) in commands
    assert ([str(fallback_venv_python), "-m", "findmyjob", "db", "upgrade", "--workspace", str(fallback_workspace)], fallback_workspace) in commands
    assert ([str(fallback_console), "version", "--json"], fallback_venv) in json_commands
    assert ([str(fallback_venv_python), "-m", "findmyjob", "greenhouse", "benchmark-results", "--json", "--workspace", str(fallback_workspace)], fallback_workspace) in json_commands
