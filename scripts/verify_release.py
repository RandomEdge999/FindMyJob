from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Iterable


_SANITIZED_PYTHON_ENV_KEYS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "__PYVENV_LAUNCHER__",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and verify Find My Job release artifacts.")
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[1], type=Path, help="Repository root.")
    parser.add_argument("--dist-dir", default=Path("dist"), type=Path, help="Directory used for build artifacts.")
    parser.add_argument(
        "--workspace-dir",
        default=None,
        type=Path,
        help="Workspace used for clean-install verification. Defaults to a fresh unique path under .tmp for each run.",
    )
    parser.add_argument(
        "--venv-dir",
        default=None,
        type=Path,
        help="Virtual environment used for artifact installation. Defaults to a fresh unique path under .tmp for each run.",
    )
    parser.add_argument("--skip-build", action="store_true", help="Reuse an existing dist directory instead of rebuilding artifacts.")
    parser.add_argument("--ignore-requires-python", action="store_true", help="Allow local verification with a non-matching interpreter.")
    return parser.parse_args()


def resolve_path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (root / value).resolve()


def fresh_sibling_path(path: Path) -> Path:
    timestamp = int(time.time() * 1000)
    for attempt in range(1, 1000):
        candidate = path.parent / f"{path.name}.fresh-{os.getpid()}-{timestamp}-{attempt}"
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Could not allocate a fresh fallback path near {path}")


def reset_dir(path: Path, *, project_root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise SystemExit(f"Refusing to reset a path outside the project root: {resolved}") from exc
    if resolved.exists():
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            fallback = fresh_sibling_path(resolved)
            print(f"Warning: could not fully reset {resolved}; using fresh path {fallback} instead. Original error: {exc}")
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SANITIZED_PYTHON_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    return env


def run(command: list[str], *, cwd: Path | None = None, expected_codes: Iterable[int] = (0,)) -> subprocess.CompletedProcess[str]:
    printable = " ".join(command)
    print(f"$ {printable}")
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=subprocess_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode not in set(expected_codes):
        raise SystemExit(f"Command failed with exit code {completed.returncode}: {printable}")
    return completed


def latest_artifact(dist_dir: Path, pattern: str) -> Path:
    matches = sorted(dist_dir.glob(pattern))
    if not matches:
        raise SystemExit(f"No artifact matched {pattern} in {dist_dir}")
    return matches[-1]


def console_script_path(venv_dir: Path) -> Path:
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    candidates = ["fmj.exe", "fmj.cmd", "fmj"] if os.name == "nt" else ["fmj"]
    for name in candidates:
        candidate = scripts_dir / name
        if candidate.exists():
            return candidate
    raise SystemExit(f"Could not locate the fmj console script in {scripts_dir}")


def json_command(command: list[str], *, cwd: Path | None = None, expected_codes: Iterable[int] = (0,)) -> object:
    completed = run(command, cwd=cwd, expected_codes=expected_codes)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Command did not produce valid JSON: {' '.join(command)}") from exc


def module_command(python_exe: Path, *args: str) -> list[str]:
    return [str(python_exe), "-m", "findmyjob", *args]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    dist_dir = resolve_path(project_root, args.dist_dir)
    workspace_dir = (
        resolve_path(project_root, args.workspace_dir)
        if args.workspace_dir is not None
        else fresh_sibling_path(project_root / ".tmp" / "release-workspace")
    )
    venv_dir = (
        resolve_path(project_root, args.venv_dir)
        if args.venv_dir is not None
        else fresh_sibling_path(project_root / ".tmp" / "release-venv")
    )

    print(f"Verification workspace: {workspace_dir}")
    print(f"Verification venv: {venv_dir}")

    if args.skip_build:
        dist_dir.mkdir(parents=True, exist_ok=True)
    else:
        dist_dir = reset_dir(dist_dir, project_root=project_root)
        run([sys.executable, "-m", "build", "--sdist", "--wheel", "--no-isolation", "--outdir", str(dist_dir)], cwd=project_root)

    wheel_path = latest_artifact(dist_dir, "findmyjob-*.whl")
    sdist_path = latest_artifact(dist_dir, "findmyjob-*.tar.gz")
    print(f"Using wheel: {wheel_path}")
    print(f"Using sdist: {sdist_path}")

    venv_dir = reset_dir(venv_dir, project_root=project_root)
    venv.EnvBuilder(with_pip=True, clear=True, system_site_packages=False).create(venv_dir)
    venv_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")

    install_command = [str(venv_python), "-m", "pip", "install"]
    if args.ignore_requires_python:
        install_command.append("--ignore-requires-python")
    install_command.append(str(wheel_path))
    run(install_command, cwd=venv_dir)
    if not args.ignore_requires_python:
        run([str(venv_python), "-m", "pip", "check"], cwd=venv_dir)

    imported_version = run([str(venv_python), "-c", "import findmyjob; print(findmyjob.__version__)"], cwd=venv_dir).stdout.strip()
    console_script = console_script_path(venv_dir)
    module_version = json_command(module_command(venv_python, "version", "--json"), cwd=venv_dir)
    cli_version = json_command([str(console_script), "version", "--json"], cwd=venv_dir)
    require(isinstance(module_version, dict), "Module version output was not a JSON object.")
    require(isinstance(cli_version, dict), "CLI version output was not a JSON object.")
    require(module_version["version"] == imported_version, "Installed module version did not match CLI version.")
    require(cli_version["version"] == imported_version, "Console script version did not match installed package version.")
    package_payload = json_command(
        [
            str(venv_python),
            "-c",
            (
                "import json, pathlib, findmyjob; "
                "pkg = pathlib.Path(findmyjob.__file__).resolve().parent; "
                "payload = {"
                "'package_dir': str(pkg), "
                "'frontend_index': str(pkg / 'web' / 'frontend_dist' / 'index.html'), "
                "'frontend_index_exists': (pkg / 'web' / 'frontend_dist' / 'index.html').exists()"
                "}; "
                "print(json.dumps(payload))"
            ),
        ],
        cwd=venv_dir,
    )
    require(isinstance(package_payload, dict), "Installed package inspection output was not a JSON object.")
    require(bool(package_payload.get("frontend_index_exists")), "Installed wheel does not contain the bundled frontend index.html.")

    workspace_dir = reset_dir(workspace_dir, project_root=project_root)
    run(module_command(venv_python, "init", "--workspace", str(workspace_dir)), cwd=workspace_dir)
    require((workspace_dir / ".fmj" / "config.toml").exists(), "fmj init did not create the workspace config file.")
    require((workspace_dir / ".fmj" / "local-overrides" / "filefirst" / "user-profile.template.yml").exists(), "fmj init did not seed the local user profile template.")
    require((workspace_dir / "templates" / "typst" / "resume.typ").exists(), "fmj init did not seed resume.typ.")
    require((workspace_dir / "templates" / "typst" / "cover_letter.typ").exists(), "fmj init did not seed cover_letter.typ.")
    workspace_config = (workspace_dir / ".fmj" / "config.toml").read_text(encoding="utf-8")
    require('default_application_mode = "dry_run"' in workspace_config, "Default workspace config should start in dry_run mode.")
    require('require_human_review_for_submit = true' in workspace_config, "Default workspace config should require human review for submit.")
    require(workspace_config.count('submit_enabled = false') >= 3, "Default workspace config should disable submit on public source defaults.")

    run(module_command(venv_python, "db", "upgrade", "--workspace", str(workspace_dir)), cwd=workspace_dir)
    revision_payload = json_command(module_command(venv_python, "db", "current", "--json", "--workspace", str(workspace_dir)), cwd=workspace_dir)
    require(isinstance(revision_payload, dict) and bool(revision_payload.get("revision")), "Database bootstrap did not report an Alembic revision.")

    config_payload = json_command(module_command(venv_python, "config", "validate", "--json", "--workspace", str(workspace_dir)), cwd=workspace_dir)
    require(isinstance(config_payload, dict), "Config validation output was not a JSON object.")
    require(config_payload["overall_status"] in {"ok", "warnings"}, "Clean workspace config validation returned an unexpected status.")

    smoke_payload = json_command(module_command(venv_python, "greenhouse", "smoke-results", "--json", "--workspace", str(workspace_dir)), cwd=workspace_dir)
    benchmark_payload = json_command(module_command(venv_python, "greenhouse", "benchmark-results", "--json", "--workspace", str(workspace_dir)), cwd=workspace_dir)
    require(isinstance(smoke_payload, list), "Smoke results output was not a JSON array.")
    require(isinstance(benchmark_payload, list), "Benchmark results output was not a JSON array.")

    doctor_payload = json_command(module_command(venv_python, "doctor", "--json", "--workspace", str(workspace_dir)), cwd=workspace_dir, expected_codes=(0, 1))
    require(isinstance(doctor_payload, dict), "Doctor output was not a JSON object.")
    require(doctor_payload["report"]["overall_status"] in {"warnings", "blocked"}, "Doctor should remain non-green on a clean workspace without launch prerequisites.")

    launch_payload = json_command(module_command(venv_python, "launch-check", "--json", "--workspace", str(workspace_dir)), cwd=workspace_dir, expected_codes=(1,))
    require(isinstance(launch_payload, dict), "Launch-check output was not a JSON object.")
    require(launch_payload["overall_status"] == "fail", "Launch-check should fail on a clean workspace without personal launch data.")

    print(
        "Release artifact verification completed successfully. "
        "Clean sample launch-check failure is expected until local candidate data and live launch prerequisites are configured."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





