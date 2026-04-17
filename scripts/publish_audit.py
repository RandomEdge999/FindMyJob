from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import yaml

TEXT_SUFFIXES = {
    ".bat",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".tsx",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_DIR_NAMES = {
    ".claude",
    ".fmj",
    ".git",
    ".github-cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    ".venv312",
    "__pycache__",
    "build",
    "data",
    "dist",
    "my_personal_information",
    "node_modules",
    "output",
    "reports",
}
EXCLUDED_PREFIXES = ("tmp", "tempbase", "test-temp", "pytest_tmp")
DISALLOWED_TRACKED_PREFIXES = (
    ".fmj/",
    "data/",
    "output/",
    "reports/",
    "my_personal_information/",
)
DISALLOWED_TRACKED_CONTAINS = (
    "/browser/",
    "/runtime/",
    "/chatgpt-downloads/",
)
DISALLOWED_TRACKED_NAMES = {
    ".env",
}
DISALLOWED_TRACKED_PATTERNS = (
    re.compile(r"(^|/)\.tmp"),
    re.compile(r"(^|/)tmp"),
    re.compile(r"(^|/)tempbase"),
    re.compile(r"(^|/)test-temp"),
    re.compile(r"(^|/)pytest_tmp"),
    re.compile(r".*\.(err|out)\.log$", re.IGNORECASE),
)
SAFE_PLACEHOLDER_PREFIXES = (
    "<",
    "example",
    "placeholder",
    "replace",
    "sample",
    "your_",
    "your-",
    "your ",
    "changeme",
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^(?:export\s+)?([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*=\s*(.+)$"
)
INLINE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-+/=]{8,})"
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)>'\"]+", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


@dataclass(slots=True)
class Finding:
    kind: str
    path: str
    detail: str
    line: int | None = None


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().strip('"\'').strip().casefold()
    return any(lowered.startswith(prefix) for prefix in SAFE_PLACEHOLDER_PREFIXES)


def _is_env_var_name(value: str) -> bool:
    stripped = value.strip().strip('"\'')
    return bool(stripped) and re.fullmatch(r"[A-Z0-9_]+", stripped) is not None


def _iter_public_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if any(part in EXCLUDED_DIR_NAMES for part in parts):
            continue
        if any(part.startswith(prefix) for prefix in EXCLUDED_PREFIXES for part in parts):
            continue
        if rel.name.startswith(".tmp"):
            continue
        if rel.suffix.lower() not in TEXT_SUFFIXES and rel.name not in {".gitignore", "README"}:
            continue
        yield path


def _iter_git_tracked_files(root: Path) -> list[Path] | None:
    git_dir = root / ".git"
    if not git_dir.exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            text=False,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    paths: list[Path] = []
    for chunk in completed.stdout.split(b"\x00"):
        if not chunk:
            continue
        candidate = (root / chunk.decode("utf-8")).resolve()
        if candidate.is_file():
            paths.append(candidate)
    return paths


def _load_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return None


def _collect_local_override_fingerprints(root: Path) -> list[tuple[str, str]]:
    fingerprints: list[tuple[str, str]] = []
    override_root = root / ".fmj" / "local-overrides" / "filefirst"
    profile_path = override_root / "config" / "profile.yml"
    facts_path = override_root / "profile" / "facts.yml"
    cv_path = override_root / "cv.md"

    if profile_path.exists():
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        candidate = payload.get("candidate", {}) if isinstance(payload, dict) else {}
        for key in ("name", "email", "phone", "linkedin", "github", "website", "location"):
            cleaned = str(candidate.get(key) or "").strip()
            if len(cleaned) >= 5 and not _is_placeholder(cleaned):
                fingerprints.append((f"local_override:profile:{key}", cleaned))

    if facts_path.exists():
        payload = yaml.safe_load(facts_path.read_text(encoding="utf-8")) or {}
        rows = payload.get("facts", []) if isinstance(payload, dict) else payload
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "").strip().lower()
            fact_id = str(row.get("fact_id") or "fact").strip()
            fact_payload = row.get("payload") or {}
            if not isinstance(fact_payload, dict):
                continue
            keys: tuple[str, ...]
            if kind == "contact":
                keys = ("name", "email", "phone", "linkedin", "github", "portfolio", "website")
            elif kind == "location":
                keys = ("display", "city")
            elif kind == "education":
                keys = ("school",)
            else:
                keys = ()
            for key in keys:
                cleaned = str(fact_payload.get(key) or "").strip()
                if len(cleaned) >= 5 and not _is_placeholder(cleaned):
                    fingerprints.append((f"local_override:{fact_id}:{key}", cleaned))

    if cv_path.exists():
        text = _load_text(cv_path) or ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and lines[0].startswith("# "):
            heading = lines[0].removeprefix("# ").strip()
            if len(heading) >= 5 and not _is_placeholder(heading):
                fingerprints.append(("local_override:cv:heading", heading))
        for line in lines[1:8]:
            cleaned = line.lstrip("- ").strip()
            if len(cleaned) < 5 or _is_placeholder(cleaned):
                continue
            if EMAIL_RE.search(cleaned) or URL_RE.search(cleaned) or PHONE_RE.search(cleaned):
                fingerprints.append(("local_override:cv:contact", cleaned))
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, value in fingerprints:
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append((label, value))
    return deduped


def _line_number(text: str, needle: str) -> int | None:
    index = text.casefold().find(needle.casefold())
    if index < 0:
        return None
    return text.count("\n", 0, index) + 1


def _scan_file(root: Path, path: Path, text: str, local_fingerprints: list[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    rel = str(path.relative_to(root))
    scan_secret_assignments = path.suffix.lower() in {".env", ".example", ".json", ".md", ".ps1", ".sh", ".toml", ".txt", ".yaml", ".yml"}
    scan_inline_secrets = path.suffix.lower() in {".env", ".example", ".json", ".md", ".ps1", ".sh", ".toml", ".txt", ".yaml", ".yml"}

    if scan_secret_assignments:
        for match in SECRET_ASSIGNMENT_RE.finditer(text):
            value = match.group(2).strip()
            if _is_placeholder(value) or _is_env_var_name(value):
                continue
            findings.append(Finding("secret_assignment", rel, f"suspicious assignment for {match.group(1)}", _line_number(text, match.group(0))))

    if scan_inline_secrets:
        for match in INLINE_SECRET_RE.finditer(text):
            value = match.group(2).strip()
            if _is_placeholder(value) or _is_env_var_name(value):
                continue
            findings.append(Finding("inline_secret", rel, f"suspicious inline {match.group(1)} value", _line_number(text, match.group(0))))

    repo_root_text = str(root.resolve())
    home_text = str(Path.home().resolve())
    path_patterns = [
        ("workspace_path", repo_root_text),
        ("home_path", home_text),
    ]
    for kind, marker in path_patterns:
        if marker and marker.casefold() in text.casefold():
            findings.append(Finding(kind, rel, "tracked file contains a machine-specific absolute path", _line_number(text, marker)))
    if re.search(r"[A-Za-z]:\\[^\\\r\n]*findmyjob[^\\\r\n]*", text):
        findings.append(Finding("workspace_path", rel, "tracked file contains a Windows absolute repo path", _line_number(text, "findmyjob")))

    for label, fingerprint in local_fingerprints:
        if fingerprint.casefold() in text.casefold():
            findings.append(Finding("local_identity_leak", rel, f"tracked file matches ignored local override value ({label})", _line_number(text, fingerprint)))

    return findings


def _path_findings(root: Path, path: Path) -> list[Finding]:
    rel = str(path.relative_to(root)).replace("\\", "/")
    findings: list[Finding] = []
    if rel in DISALLOWED_TRACKED_NAMES:
        findings.append(Finding("tracked_operator_state", rel, "tracked file is disallowed on the public repo boundary"))
        return findings
    if any(rel.startswith(prefix) for prefix in DISALLOWED_TRACKED_PREFIXES):
        findings.append(Finding("tracked_operator_state", rel, "tracked operator/runtime state path is disallowed in the public repo"))
    if any(marker in f"/{rel}" for marker in DISALLOWED_TRACKED_CONTAINS):
        findings.append(Finding("tracked_operator_state", rel, "tracked browser/runtime cache path is disallowed in the public repo"))
    if any(pattern.search(rel) for pattern in DISALLOWED_TRACKED_PATTERNS):
        findings.append(Finding("tracked_scratch_file", rel, "tracked scratch/debug artifact is disallowed in the public repo"))
    return findings


def run_audit(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    local_fingerprints = _collect_local_override_fingerprints(root)
    tracked_paths = _iter_git_tracked_files(root)
    candidate_paths = tracked_paths if tracked_paths is not None else list(_iter_public_files(root))
    for path in candidate_paths:
        findings.extend(_path_findings(root, path))
        text = _load_text(path)
        if text is None:
            continue
        findings.extend(_scan_file(root, path, text, local_fingerprints))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the public repo surface for secrets, local identity leaks, and machine-specific paths.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    args = parser.parse_args()

    root = args.root.resolve()
    findings = run_audit(root)

    if args.json:
        payload = {
            "root": str(root),
            "finding_count": len(findings),
            "findings": [asdict(finding) for finding in findings],
        }
        print(json.dumps(payload, indent=2))
    elif findings:
        print("Publish audit failed:")
        for finding in findings:
            line = f":{finding.line}" if finding.line is not None else ""
            print(f"- [{finding.kind}] {finding.path}{line} :: {finding.detail}")
    else:
        print("Publish audit passed. No tracked secret, identity, or machine-path leaks were found.")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
