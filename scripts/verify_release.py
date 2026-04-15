from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and verify Find My Job release artifacts.")
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[1], type=Path, help="Repository root.")
    parser.add_argument("--dist-dir", default=Path("dist"), type=Path, help="Directory used for build artifacts.")
    parser.add_argument("--workspace-dir", default=Path(".tmp/release-workspace"), type=Path, help="Workspace used for clean-install verification.")
    parser.add_argument("--venv-dir", default=Path(".tmp/release-venv"), type=Path, help="Virtual environment used for artifact installation.")
    parser.add_argument("--skip-build", action="store_true", help="Reuse an existing dist directory instead of rebuilding artifacts.")
    parser.add_argument("--ignore-requires-python", action="store_true", help="Allow local verification with a non-matching interpreter.")
    return parser.parse_args()


def resolve_path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (root / value).resolve()


def reset_dir(path: Path, *, project_root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise SystemExit(f"Refusing to reset a path outside the project root: {resolved}") from exc
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def run(command: list[str], *, cwd: Path | None = None, expected_codes: Iterable[int] = (0,)) -> subprocess.CompletedProcess[str]:
    printable = " ".join(command)
    print(f"$ {printable}")
    completed = subprocess.run(command, cwd=str(cwd) if cwd is not None else None, text=True, capture_output=True, check=False)
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


def json_command(command: list[str], *, expected_codes: Iterable[int] = (0,)) -> object:
    completed = run(command, expected_codes=expected_codes)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Command did not produce valid JSON: {' '.join(command)}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    dist_dir = resolve_path(project_root, args.dist_dir)
    workspace_dir = resolve_path(project_root, args.workspace_dir)
    venv_dir = resolve_path(project_root, args.venv_dir)

    if args.skip_build:
        dist_dir.mkdir(parents=True, exist_ok=True)
    else:
        reset_dir(dist_dir, project_root=project_root)
        run([sys.executable, "-m", "build", "--sdist", "--wheel", "--no-isolation", "--outdir", str(dist_dir)], cwd=project_root)

    wheel_path = latest_artifact(dist_dir, "findmyjob-*.whl")
    sdist_path = latest_artifact(dist_dir, "findmyjob-*.tar.gz")
    print(f"Using wheel: {wheel_path}")
    print(f"Using sdist: {sdist_path}")

    reset_dir(venv_dir, project_root=project_root)
    venv.EnvBuilder(with_pip=True, clear=True, system_site_packages=False).create(venv_dir)
    venv_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")

    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
    install_command = [str(venv_python), "-m", "pip", "install"]
    if args.ignore_requires_python:
        install_command.append("--ignore-requires-python")
    install_command.append(str(wheel_path))
    run(install_command)
    if not args.ignore_requires_python:
        run([str(venv_python), "-m", "pip", "check"])

    imported_version = run([str(venv_python), "-c", "import findmyjob; print(findmyjob.__version__)"]).stdout.strip()
    console_script = console_script_path(venv_dir)
    module_version = json_command([str(venv_python), "-m", "findmyjob", "version", "--json"])
    cli_version = json_command([str(console_script), "version", "--json"])
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
        ]
    )
    require(isinstance(package_payload, dict), "Installed package inspection output was not a JSON object.")
    require(bool(package_payload.get("frontend_index_exists")), "Installed wheel does not contain the bundled frontend index.html.")

    reset_dir(workspace_dir, project_root=project_root)
    run([str(console_script), "init", "--workspace", str(workspace_dir)])
    require((workspace_dir / ".fmj" / "config.toml").exists(), "fmj init did not create the workspace config file.")
    require((workspace_dir / ".fmj" / "local-overrides" / "filefirst" / "user-profile.template.yml").exists(), "fmj init did not seed the local user profile template.")
    require((workspace_dir / "templates" / "typst" / "resume.typ").exists(), "fmj init did not seed resume.typ.")
    require((workspace_dir / "templates" / "typst" / "cover_letter.typ").exists(), "fmj init did not seed cover_letter.typ.")
    workspace_config = (workspace_dir / ".fmj" / "config.toml").read_text(encoding="utf-8")
    require('default_application_mode = "dry_run"' in workspace_config, "Default workspace config should start in dry_run mode.")
    require('require_human_review_for_submit = true' in workspace_config, "Default workspace config should require human review for submit.")
    require(workspace_config.count('submit_enabled = false') >= 3, "Default workspace config should disable submit on public source defaults.")

    run([str(console_script), "db", "upgrade", "--workspace", str(workspace_dir)])
    revision_payload = json_command([str(console_script), "db", "current", "--json", "--workspace", str(workspace_dir)])
    require(isinstance(revision_payload, dict) and bool(revision_payload.get("revision")), "Database bootstrap did not report an Alembic revision.")

    config_payload = json_command([str(console_script), "config", "validate", "--json", "--workspace", str(workspace_dir)])
    require(isinstance(config_payload, dict), "Config validation output was not a JSON object.")
    require(config_payload["overall_status"] in {"ok", "warnings"}, "Clean workspace config validation returned an unexpected status.")

    smoke_payload = json_command([str(console_script), "greenhouse", "smoke-results", "--json", "--workspace", str(workspace_dir)])
    benchmark_payload = json_command([str(console_script), "greenhouse", "benchmark-results", "--json", "--workspace", str(workspace_dir)])
    require(isinstance(smoke_payload, list), "Smoke results output was not a JSON array.")
    require(isinstance(benchmark_payload, list), "Benchmark results output was not a JSON array.")

    doctor_payload = json_command([str(console_script), "doctor", "--json", "--workspace", str(workspace_dir)], expected_codes=(0, 1))
    require(isinstance(doctor_payload, dict), "Doctor output was not a JSON object.")
    require(doctor_payload["report"]["overall_status"] in {"warnings", "blocked"}, "Doctor should remain non-green on a clean workspace without launch prerequisites.")

    launch_payload = json_command([str(console_script), "launch-check", "--json", "--workspace", str(workspace_dir)], expected_codes=(1,))
    require(isinstance(launch_payload, dict), "Launch-check output was not a JSON object.")
    require(launch_payload["overall_status"] == "fail", "Launch-check should fail on a clean workspace without personal launch data.")

    print("Release artifact verification completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





