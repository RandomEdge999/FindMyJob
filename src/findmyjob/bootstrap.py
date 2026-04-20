from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

BOOTSTRAP_VERSION = 1
DEFAULT_VENV_NAME = ".venv312"


class BootstrapError(RuntimeError):
    pass


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_project_root(project_root: Path | str | None = None) -> Path:
    root = Path(project_root) if project_root is not None else default_project_root()
    resolved = root.resolve()
    if not (resolved / "pyproject.toml").exists():
        raise BootstrapError(f"Could not find pyproject.toml under {resolved}.")
    return resolved


def resolve_venv_dir(project_root: Path, venv_name: str = DEFAULT_VENV_NAME, venv_dir: Path | str | None = None) -> Path:
    if venv_dir is not None:
        return Path(venv_dir).resolve()
    return (project_root / venv_name).resolve()


def venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def bootstrap_stamp_path(venv_dir: Path) -> Path:
    return venv_dir / ".findmyjob-bootstrap.json"


def bootstrap_signature(project_root: Path, *, include_playwright: bool, install_playwright_browser: bool) -> dict[str, Any]:
    pyproject = project_root / "pyproject.toml"
    return {
        "bootstrap_version": BOOTSTRAP_VERSION,
        "pyproject_mtime_ns": pyproject.stat().st_mtime_ns,
        "include_playwright": include_playwright,
        "install_playwright_browser": install_playwright_browser,
        "package_spec": editable_package_spec(include_playwright=include_playwright),
    }


def editable_package_spec(*, include_playwright: bool) -> str:
    return ".[playwright]" if include_playwright else "."


def load_bootstrap_stamp(stamp_path: Path) -> dict[str, Any] | None:
    if not stamp_path.exists():
        return None
    try:
        payload = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_bootstrap_stamp(stamp_path: Path, payload: dict[str, Any]) -> None:
    stamp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PYTHONNOUSERSITE", "1")
    return env


def run_subprocess(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(command, cwd=str(cwd), env=_subprocess_env(), check=False)
    if completed.returncode != 0:
        raise BootstrapError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def probe_environment(venv_python: Path, *, project_root: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "python_exists": venv_python.exists(),
        "findmyjob_installed": False,
        "editable_checkout": False,
        "playwright_installed": False,
        "findmyjob_path": None,
    }
    if not venv_python.exists():
        return payload

    script = (
        "import json, pathlib, sys\n"
        "project_root = pathlib.Path(sys.argv[1]).resolve()\n"
        "project_src = (project_root / 'src').resolve()\n"
        "payload = {\n"
        "    'findmyjob_installed': False,\n"
        "    'editable_checkout': False,\n"
        "    'playwright_installed': False,\n"
        "    'findmyjob_path': None,\n"
        "}\n"
        "try:\n"
        "    import findmyjob\n"
        "    payload['findmyjob_installed'] = True\n"
        "    package_path = pathlib.Path(findmyjob.__file__).resolve()\n"
        "    payload['findmyjob_path'] = str(package_path)\n"
        "    payload['editable_checkout'] = project_src in package_path.parents\n"
        "except Exception as exc:\n"
        "    payload['findmyjob_error'] = f'{type(exc).__name__}: {exc}'\n"
        "try:\n"
        "    import playwright\n"
        "    payload['playwright_installed'] = True\n"
        "except Exception as exc:\n"
        "    payload['playwright_error'] = f'{type(exc).__name__}: {exc}'\n"
        "print(json.dumps(payload))\n"
    )
    completed = subprocess.run(
        [str(venv_python), "-c", script, str(project_root)],
        cwd=str(project_root),
        env=_subprocess_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        payload["probe_error"] = completed.stderr.strip() or completed.stdout.strip() or "probe failed"
        return payload
    try:
        probed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload["probe_error"] = completed.stdout.strip() or "invalid probe output"
        return payload
    if isinstance(probed, dict):
        payload.update(probed)
    return payload


def should_install_package(
    environment: dict[str, Any],
    stamp_payload: dict[str, Any] | None,
    signature: dict[str, Any],
    *,
    include_playwright: bool,
) -> tuple[bool, str]:
    if not environment.get("python_exists"):
        return True, "venv_missing"
    if not environment.get("findmyjob_installed"):
        return True, "package_missing"
    if not environment.get("editable_checkout"):
        return True, "package_not_editable"
    if include_playwright and not environment.get("playwright_installed"):
        return True, "playwright_missing"
    if stamp_payload != signature:
        return True, "bootstrap_stamp_stale"
    return False, "already_ready"


def _require_python_312() -> str:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if version != "3.12":
        raise BootstrapError(
            "FindMyJob bootstrap requires Python 3.12. "
            f"Current interpreter is {version} at {Path(sys.executable).resolve()}."
        )
    return version


def bootstrap_repo_environment(
    *,
    project_root: Path | str | None = None,
    venv_name: str = DEFAULT_VENV_NAME,
    venv_dir: Path | str | None = None,
    include_playwright: bool = True,
    install_playwright_browser: bool = True,
    force_install: bool = False,
) -> dict[str, Any]:
    python_version = _require_python_312()
    resolved_root = resolve_project_root(project_root)
    resolved_venv_dir = resolve_venv_dir(resolved_root, venv_name=venv_name, venv_dir=venv_dir)
    resolved_venv_dir.parent.mkdir(parents=True, exist_ok=True)
    created_venv = False
    if not venv_python_path(resolved_venv_dir).exists():
        venv.EnvBuilder(with_pip=True, clear=False, system_site_packages=False).create(resolved_venv_dir)
        created_venv = True

    resolved_venv_python = venv_python_path(resolved_venv_dir)
    stamp_path = bootstrap_stamp_path(resolved_venv_dir)
    signature = bootstrap_signature(
        resolved_root,
        include_playwright=include_playwright,
        install_playwright_browser=install_playwright_browser,
    )
    existing_stamp = load_bootstrap_stamp(stamp_path)
    environment = probe_environment(resolved_venv_python, project_root=resolved_root)
    install_needed, reason = should_install_package(
        environment,
        existing_stamp,
        signature,
        include_playwright=include_playwright,
    )
    if force_install:
        install_needed = True
        reason = "force_install"

    installed_package = False
    installed_playwright = False
    if install_needed:
        run_subprocess([str(resolved_venv_python), "-m", "pip", "install", "--upgrade", "pip"], cwd=resolved_root)
        install_command = [
            str(resolved_venv_python),
            "-m",
            "pip",
            "install",
            "-e",
            editable_package_spec(include_playwright=include_playwright),
        ]
        run_subprocess(install_command, cwd=resolved_root)
        installed_package = True
        if include_playwright and install_playwright_browser:
            run_subprocess([str(resolved_venv_python), "-m", "playwright", "install", "chromium"], cwd=resolved_root)
            installed_playwright = True
        write_bootstrap_stamp(stamp_path, signature)

    final_environment = probe_environment(resolved_venv_python, project_root=resolved_root)
    if not final_environment.get("findmyjob_installed"):
        raise BootstrapError(
            "The repo-local environment was created, but FindMyJob is still not importable from that environment."
        )

    if include_playwright and not final_environment.get("playwright_installed"):
        raise BootstrapError(
            "The repo-local environment is missing Playwright. Re-run bootstrap or install `.[playwright]` manually."
        )

    summary = (
        f"Repo-local Python {python_version} environment ready at {resolved_venv_python}. "
        f"Install reason: {reason}."
    )
    return {
        "status": "ready",
        "summary": summary,
        "project_root": str(resolved_root),
        "venv_dir": str(resolved_venv_dir),
        "venv_python": str(resolved_venv_python),
        "created_venv": created_venv,
        "installed_package": installed_package,
        "installed_playwright_browser": installed_playwright,
        "python": python_version,
        "install_reason": reason,
        "package_spec": editable_package_spec(include_playwright=include_playwright),
        "environment": final_environment,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or repair the repo-local FindMyJob environment.")
    parser.add_argument("--project-root", default=default_project_root(), type=Path, help="Repository root.")
    parser.add_argument("--venv-name", default=DEFAULT_VENV_NAME, help="Preferred repo-local virtual environment directory name.")
    parser.add_argument("--venv-dir", default=None, type=Path, help="Override the repo-local virtual environment path.")
    parser.add_argument(
        "--install-playwright-browser",
        dest="install_playwright_browser",
        action="store_true",
        default=True,
        help="Install Chromium into the repo-local environment when dependencies are installed.",
    )
    parser.add_argument(
        "--skip-playwright-browser",
        dest="install_playwright_browser",
        action="store_false",
        help="Skip `python -m playwright install chromium` during bootstrap.",
    )
    parser.add_argument("--force-install", action="store_true", help="Reinstall the editable package even if the repo-local environment already looks ready.")
    parser.add_argument("--json", action="store_true", help="Emit bootstrap details as JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = bootstrap_repo_environment(
            project_root=args.project_root,
            venv_name=args.venv_name,
            venv_dir=args.venv_dir,
            include_playwright=True,
            install_playwright_browser=args.install_playwright_browser,
            force_install=args.force_install,
        )
    except BootstrapError as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(payload["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())