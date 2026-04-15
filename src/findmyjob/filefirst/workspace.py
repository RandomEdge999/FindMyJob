from __future__ import annotations

import csv
from copy import deepcopy
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import threading
from typing import Any

import yaml
from pydantic import ValidationError

from findmyjob.core.config import write_default_workspace_config
from findmyjob.core.lmstudio import LMSTUDIO_AUTO_MODEL, LMSTUDIO_DEFAULT_HOST, LMSTUDIO_PROVIDER
from findmyjob.filefirst.models import (
    AnswerMemoryEntry,
    ApplicationEntry,
    BoardDiscoveryState,
    EvaluationResult,
    FileFact,
    InboxJob,
    PortalsConfig,
    LiveRunEvent,
    LiveRunState,
    RunRecord,
    ScreeningDecision,
    SubmissionRecord,
    WorkspaceProfile,
    utcnow_iso,
)
from findmyjob.filefirst.portal_defaults import default_portals_payload
from findmyjob.sources.normalizer import slugify

DEFAULT_CV = """# Candidate CV

Add your canonical CV here in markdown. This becomes the source of truth for the mode runner.
"""

DEFAULT_LOCAL_USER_PROFILE_TEMPLATE = """# Local user profile for FindMyJob
#
# Copy this file to `.fmj/local-overrides/filefirst/user-profile.yml` and fill in
# your own details. This file is local-only and ignored by Git.
#
# The public repo ships fictional tracked sample data. This local file is the
# recommended place to put your real candidate information without editing
# tracked files directly.

candidate:
  name: Your Name
  email: you@example.com
  phone: ""
  location: City, State, Country
  linkedin: ""
  github: ""
  website: ""
  summary: ""
  target_roles:
    - Software Engineer

targets:
  title_keywords:
    - software engineer
    - backend engineer
    - python engineer
  locations:
    - Remote - United States
  countries:
    - US
  regions: []
  cities: []
  remote_only: true
  employment_types:
    - full-time
  excluded_keywords:
    - staff
    - principal
  blocked_companies: []
  posted_within_days: 30

authorization:
  is_authorized: true
  requires_future_sponsorship: false

education:
  - school: Sample University
    degree: B.S.
    field: Computer Science
    graduation_date: ""
    location: ""

languages:
  - name: English
    fluency: Fluent

resume:
  markdown_path: ""
  dossier_path: ""

default_answers:
  - question: Are you fluent in English?
    answer: Yes, I am fluent in English.

facts:
  work: []
  projects: []
  skills: []
"""

DEFAULT_PROFILE = {
    "candidate": {
        "name": "",
        "email": None,
        "phone": None,
        "location": None,
        "linkedin": None,
        "github": None,
        "website": None,
        "summary": None,
        "target_roles": [],
    },
    "targets": {
        "title_keywords": [],
        "locations": [],
        "countries": ["US"],
        "regions": [],
        "cities": [],
        "remote_only": True,
        "employment_types": [],
        "excluded_keywords": [],
        "blocked_companies": [],
        "posted_within_days": 30,
    },
    "runtime": {
        "model": {
            "provider": LMSTUDIO_PROVIDER,
            "transport": "local_http",
            "base_url": LMSTUDIO_DEFAULT_HOST,
            "api_key_env": None,
            "model": LMSTUDIO_AUTO_MODEL,
            "temperature": 0.2,
            "max_tokens": 8192,
            "preferred_context_window": 131072,
            "local": True,
            "command": [],
            "working_dir": None,
        },
        "automation": {
            "enabled": True,
            "submit_enabled": False,
            "default_submit_mode": "preview_first",
            "production_sources": ["greenhouse"],
            "ready_to_apply_threshold": 10,
            "browser_mode": "headed",
            "browser_attach_enabled": False,
            "browser_cdp_url": "http://127.0.0.1:9222",
            "max_open_tabs": 6,
            "daily_submit_cap": 100,
            "per_company_daily_cap": 2,
            "capture_traces": False,
            "capture_dom": False,
        },
    },
}

DEFAULT_PORTALS = default_portals_payload()

DEFAULT_MODE_FILES = {
    "_shared.md": """# Shared Rules

- Stay truthful to the candidate facts and CV. Do not fabricate metrics, employers, dates, titles, or tools.
- Optimize for high-fit roles only. If the match is weak, say so clearly.
- Use concise, operator-friendly output.
""",
    "eval.md": """# Eval Mode

Return JSON with these keys:
- company
- role
- archetype
- score (0.0 to 5.0)
- grade (A-F)
- summary
- keywords (list of 10-20 strings)
- fit_reasons (list of strings)
- gaps (list of strings)
- report_markdown
- resume_headline
- resume_summary_lines
- selected_work_fact_ids
- selected_project_fact_ids
- selected_skill_fact_ids
- custom_bullets
- cover_letter_paragraphs

The markdown report should contain:
- title
- role summary
- fit reasons
- gaps and mitigation
- resume strategy
- interview angles
""",
    "pdf.md": """# PDF Mode

Return JSON with these keys:
- headline
- summary_lines
- selected_work_fact_ids
- selected_project_fact_ids
- selected_skill_fact_ids
- custom_bullets
- cover_letter_paragraphs

Select only fact IDs that already exist in the provided facts.
Cover letter paragraphs must be complete sentences with no placeholders, variables, or template markers. Every paragraph must read as natural prose ready to send.
""",
    "scan.md": """# Scan Mode

Prefer direct board/API discovery over broad search fallback. Keep only roles that match the target keywords.
""",
    "screen.md": """# Screen Mode

Return JSON only. Use this exact schema:
- approved (boolean)
- reasons (list of strings)
- confidence (0.0 to 1.0)
- internship_like (boolean)
- seniority_too_high (boolean)
- years_experience_signal (string or null)
- notes (string or null)

The deterministic filter already removes obvious title rejects. Your job is to classify the remaining jobs conservatively but not narrowly.

Hard reject signals:
- Reject if the title contains: senior, sr, staff, principal, lead, architect, director, vp, head, chief, manager, distinguished, fellow.
- Reject internships, apprenticeships, co-ops, and fellowships.
- Reject only if the posting explicitly requires 7+ years of experience.
- Reject only if the posting explicitly requires a graduate degree: PhD required or Masters required. Do not reject when those degrees are only preferred.

Approve signals:
- Approve any role without an explicit seniority marker when the title or description matches any of these target keywords: engineer, developer, software, ml, machine learning, data, python, research.
- If the title does NOT contain a seniority marker (senior, sr, staff, principal, lead, architect, director, manager, vp, head, chief), default to approved=true.
- The candidate is applying broadly to early-career AND mid-level roles.
- A job requiring 2-5 years of experience IS appropriate for this candidate.

Confidence rules:
- Set confidence to 0.8 or higher when the decision is clear.
- Do not use 0.2 unless you genuinely cannot tell from the posting.
- Keep the booleans and reasons internally consistent.

Approved example:
{
  "approved": true,
  "reasons": ["Title and description match software/data targets with no seniority marker."],
  "confidence": 0.92,
  "internship_like": false,
  "seniority_too_high": false,
  "years_experience_signal": "2-5 years",
  "notes": "Broad early-career and mid-level fit."
}

Rejected example:
{
  "approved": false,
  "reasons": ["Posting explicitly requires 8+ years of experience."],
  "confidence": 0.9,
  "internship_like": false,
  "seniority_too_high": true,
  "years_experience_signal": "8+ years",
  "notes": "Explicit seniority requirement."
}
""",
    "pipeline.md": """# Pipeline Mode

Process inbox items in this order:
1. screen
2. evaluate
3. update tracker
4. generate PDF
""",
    "tracker.md": """# Tracker Mode

The tracker is the operator source of truth. Keep statuses human-readable and conservative.
""",
}

APPLICATION_HEADERS = ["#", "Job ID", "Date", "Company", "Role", "Score", "Grade", "Status", "PDF", "Report", "URL", "Source", "Notes"]
INBOX_COLUMNS = [
    "job_id",
    "company",
    "title",
    "source",
    "source_kind",
    "source_job_id",
    "url",
    "apply_url",
    "location",
    "posted_at",
    "discovered_at",
    "ats_family",
    "ats_preview_supported",
    "hard_reject_reason",
    "auth_reject_reason",
    "login_wall_detected",
    "rehearsal_eligible",
    "rehearsal_rank",
    "discovery_method",
    "screening_status",
    "screening_confidence",
    "screening_reasons",
    "screening_overridden",
    "internship_like",
    "seniority_too_high",
    "years_experience_signal",
    "workflow_state",
    "board_family",
    "automation_tier",
    "job_identity_key",
    "duplicate_cluster_key",
]
SCAN_HISTORY_COLUMNS = ["seen_at", "job_id", "url", "company", "title", "source", "duplicate_cluster_key"]



def _yaml_dump(payload: Any) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).rstrip() + "\n"


def _deep_merge_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged



def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if payload is not None else None


def _meaningful_text(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.casefold()
    if not text:
        return ""
    if lowered.startswith(("your ", "your_", "your-", "sample ", "sample_", "sample-", "example", "replace")):
        return ""
    return text


def _normalize_identifier(value: str, *, fallback: str) -> str:
    cleaned = slugify(str(value or "").strip())
    return cleaned or fallback


def _resolve_workspace_relative_path(root: Path, value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate).resolve()


def _build_user_profile_patch(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    targets = payload.get("targets") if isinstance(payload.get("targets"), dict) else {}
    patch: dict[str, Any] = {}
    if candidate:
        patch["candidate"] = candidate
    if targets:
        patch["targets"] = targets
    return patch


def _build_user_profile_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    authorization = payload.get("authorization") if isinstance(payload.get("authorization"), dict) else {}
    facts_payload = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    rows: list[dict[str, Any]] = []

    name = _meaningful_text(candidate.get("name"))
    email = _meaningful_text(candidate.get("email"))
    phone = str(candidate.get("phone") or "").strip()
    linkedin = str(candidate.get("linkedin") or "").strip()
    github = str(candidate.get("github") or "").strip()
    website = str(candidate.get("website") or "").strip()
    location = _meaningful_text(candidate.get("location"))

    if any((name, email, phone, linkedin, github, website)):
        rows.append(
            {
                "fact_id": "contact.primary",
                "kind": "contact",
                "payload": {
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "linkedin": linkedin,
                    "github": github,
                    "website": website,
                },
                "sensitivity": "medium",
                "allowed_for_generation": True,
                "disallowed": False,
                "provenance": "local_user_profile",
                "confirmed": True,
            }
        )

    if location:
        rows.append(
            {
                "fact_id": "location.primary",
                "kind": "location",
                "payload": {
                    "display": location,
                },
                "sensitivity": "low",
                "allowed_for_generation": True,
                "disallowed": False,
                "provenance": "local_user_profile",
                "confirmed": True,
            }
        )

    if authorization:
        rows.append(
            {
                "fact_id": "authorization.primary",
                "kind": "authorization",
                "payload": {
                    "is_authorized": authorization.get("is_authorized"),
                    "requires_future_sponsorship": authorization.get("requires_future_sponsorship"),
                },
                "sensitivity": "high",
                "allowed_for_generation": True,
                "disallowed": False,
                "provenance": "local_user_profile",
                "confirmed": True,
            }
        )

    for index, item in enumerate(payload.get("education") or [], start=1):
        if not isinstance(item, dict):
            continue
        school = _meaningful_text(item.get("school"))
        degree = str(item.get("degree") or "").strip()
        field = str(item.get("field") or "").strip()
        graduation_date = str(item.get("graduation_date") or "").strip()
        education_location = str(item.get("location") or "").strip()
        if not any((school, degree, field, graduation_date, education_location)):
            continue
        rows.append(
            {
                "fact_id": f"education.{index}",
                "kind": "education",
                "payload": {
                    "school": school,
                    "degree": degree,
                    "field": field,
                    "graduation_date": graduation_date,
                    "location": education_location,
                },
                "sensitivity": "medium",
                "allowed_for_generation": True,
                "disallowed": False,
                "provenance": "local_user_profile",
                "confirmed": True,
            }
        )

    for index, item in enumerate(payload.get("languages") or [], start=1):
        if not isinstance(item, dict):
            continue
        language_name = _meaningful_text(item.get("name"))
        fluency = str(item.get("fluency") or "").strip()
        if not any((language_name, fluency)):
            continue
        rows.append(
            {
                "fact_id": f"language.{index}",
                "kind": "language",
                "payload": {
                    "name": language_name,
                    "fluency": fluency,
                },
                "sensitivity": "low",
                "allowed_for_generation": True,
                "disallowed": False,
                "provenance": "local_user_profile",
                "confirmed": True,
            }
        )

    for kind, field_name in (("work", "work"), ("project", "projects"), ("skill", "skills")):
        for index, item in enumerate(facts_payload.get(field_name) or [], start=1):
            if not isinstance(item, dict):
                continue
            fact_id = _normalize_identifier(str(item.get("id") or item.get("fact_id") or "").strip(), fallback=f"{kind}.{index}")
            payload_item = {key: value for key, value in item.items() if key not in {"id", "fact_id"}}
            if not payload_item:
                continue
            rows.append(
                {
                    "fact_id": fact_id,
                    "kind": kind,
                    "payload": payload_item,
                    "sensitivity": "medium",
                    "allowed_for_generation": True,
                    "disallowed": False,
                    "provenance": "local_user_profile",
                    "confirmed": True,
                }
            )
    return rows


def _build_user_profile_answer_memory(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload.get("default_answers") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("canonical_question") or "").strip()
        answer = str(item.get("answer") or item.get("answer_text") or "").strip()
        if not question or not answer:
            continue
        rows.append(
            {
                "canonical_question": question,
                "context_constraints": item.get("context_constraints") if isinstance(item.get("context_constraints"), dict) else {},
                "answer_text": answer,
                "grounded_fact_ids": [str(value).strip() for value in item.get("grounded_fact_ids") or [] if str(value).strip()],
                "approved": bool(item.get("approved", True)),
            }
        )
    return rows


def _serialize_screening(screening: ScreeningDecision | None) -> dict[str, str]:
    if screening is None:
        return {
            "screening_status": "",
            "screening_confidence": "",
            "screening_reasons": "",
            "screening_overridden": "",
            "internship_like": "",
            "seniority_too_high": "",
            "years_experience_signal": "",
        }
    return {
        "screening_status": screening.status,
        "screening_confidence": f"{screening.confidence:.2f}",
        "screening_reasons": " | ".join(screening.reasons),
        "screening_overridden": "yes" if screening.overridden else "no",
        "internship_like": "yes" if screening.internship_like else "no",
        "seniority_too_high": "yes" if screening.seniority_too_high else "no",
        "years_experience_signal": screening.years_experience_signal or "",
    }


def _screening_from_row(row: dict[str, str]) -> ScreeningDecision | None:
    status = str(row.get("screening_status") or "").strip().lower()
    if not status:
        return None
    confidence_text = str(row.get("screening_confidence") or "").strip()
    try:
        confidence = float(confidence_text) if confidence_text else 0.0
    except ValueError:
        confidence = 0.0
    reasons = [item.strip() for item in str(row.get("screening_reasons") or "").split("|") if item.strip()]
    return ScreeningDecision(
        approved=status in {"approved", "overridden"},
        reasons=reasons,
        confidence=max(0.0, min(1.0, confidence)),
        internship_like=str(row.get("internship_like") or "").strip().lower() in {"yes", "true", "1"},
        seniority_too_high=str(row.get("seniority_too_high") or "").strip().lower() in {"yes", "true", "1"},
        years_experience_signal=str(row.get("years_experience_signal") or "").strip() or None,
        overridden=status == "overridden" or str(row.get("screening_overridden") or "").strip().lower() in {"yes", "true", "1"},
    )


def _inbox_row(job: InboxJob) -> dict[str, Any]:
    payload = job.model_dump(mode="json")
    payload.update(_serialize_screening(job.screening))
    return {column: payload.get(column, "") for column in INBOX_COLUMNS}


@dataclass(slots=True)
class FileWorkspace:
    _INBOX_LOCK = threading.Lock()
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def profile_dir(self) -> Path:
        return self.root / "profile"

    @property
    def fmj_dir(self) -> Path:
        return self.root / ".fmj"

    @property
    def runtime_dir(self) -> Path:
        return self.fmj_dir / "runtime"

    @property
    def browser_dir(self) -> Path:
        return self.fmj_dir / "browser"

    @property
    def local_overrides_dir(self) -> Path:
        return self.fmj_dir / "local-overrides" / "filefirst"

    @property
    def public_user_profile_template_path(self) -> Path:
        return self.root / "templates" / "user-profile.local.example.yml"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def evaluations_dir(self) -> Path:
        return self.data_dir / "evaluations"

    @property
    def submissions_dir(self) -> Path:
        return self.data_dir / "submissions"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"

    @property
    def live_runs_dir(self) -> Path:
        return self.live_dir / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def modes_dir(self) -> Path:
        return self.root / "modes"

    @property
    def cv_path(self) -> Path:
        return self.root / "cv.md"

    @property
    def local_cv_path(self) -> Path:
        return self.local_overrides_dir / "cv.md"

    @property
    def user_profile_path(self) -> Path:
        return self.local_overrides_dir / "user-profile.yml"

    @property
    def user_profile_template_path(self) -> Path:
        return self.local_overrides_dir / "user-profile.template.yml"

    @property
    def profile_path(self) -> Path:
        return self.config_dir / "profile.yml"

    @property
    def local_profile_path(self) -> Path:
        return self.local_overrides_dir / "config" / "profile.yml"

    @property
    def portals_path(self) -> Path:
        return self.root / "portals.yml"

    @property
    def facts_path(self) -> Path:
        return self.profile_dir / "facts.yml"

    @property
    def local_facts_path(self) -> Path:
        return self.local_overrides_dir / "profile" / "facts.yml"

    @property
    def answer_memory_path(self) -> Path:
        return self.profile_dir / "answer-memory.yml"

    @property
    def local_answer_memory_path(self) -> Path:
        return self.local_overrides_dir / "profile" / "answer-memory.yml"

    @property
    def candidate_dossier_path(self) -> Path:
        return self.profile_dir / "candidate-dossier.md"

    @property
    def local_candidate_dossier_path(self) -> Path:
        return self.local_overrides_dir / "profile" / "candidate-dossier.md"

    @property
    def workspace_config_path(self) -> Path:
        return self.fmj_dir / "config.toml"

    @property
    def handled_jobs_path(self) -> Path:
        return self.fmj_dir / "handled-jobs.json"

    @property
    def chatgpt_drafting_status_path(self) -> Path:
        return self.runtime_dir / "chatgpt-drafting-status.json"

    @property
    def live_state_path(self) -> Path:
        return self.live_dir / "state.json"

    @property
    def live_events_path(self) -> Path:
        return self.live_dir / "events.ndjson"

    @property
    def board_discovery_path(self) -> Path:
        return self.live_dir / "board-discovery.json"

    @property
    def inbox_path(self) -> Path:
        return self.data_dir / "inbox.tsv"

    @property
    def scan_history_path(self) -> Path:
        return self.data_dir / "scan-history.tsv"

    @property
    def applications_path(self) -> Path:
        return self.data_dir / "applications.md"

    def ensure(self) -> None:
        for directory in (
            self.config_dir,
            self.profile_dir,
            self.fmj_dir,
            self.runtime_dir,
            self.browser_dir,
            self.local_overrides_dir,
            self.data_dir,
            self.jobs_dir,
            self.evaluations_dir,
            self.submissions_dir,
            self.runs_dir,
            self.live_dir,
            self.live_runs_dir,
            self.reports_dir,
            self.output_dir,
            self.modes_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._write_if_missing(self.cv_path, DEFAULT_CV)
        self._write_if_missing(self.profile_path, _yaml_dump(DEFAULT_PROFILE))
        self._write_if_missing(self.portals_path, _yaml_dump(DEFAULT_PORTALS))
        self._write_if_missing(self.user_profile_template_path, DEFAULT_LOCAL_USER_PROFILE_TEMPLATE.rstrip() + "\n")
        if not self.workspace_config_path.exists():
            write_default_workspace_config(self.workspace_config_path)
        self._write_if_missing(self.facts_path, _yaml_dump({"facts": []}))
        self._write_if_missing(self.answer_memory_path, _yaml_dump({"answers": []}))
        if not self.board_discovery_path.exists():
            self.board_discovery_path.write_text(
                json.dumps(BoardDiscoveryState().model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if not self.inbox_path.exists():
            self._write_tsv(self.inbox_path, INBOX_COLUMNS, [])
        if not self.scan_history_path.exists():
            self._write_tsv(self.scan_history_path, SCAN_HISTORY_COLUMNS, [])
        if not self.applications_path.exists():
            self.save_applications([])
        for filename, content in DEFAULT_MODE_FILES.items():
            self._write_if_missing(self.modes_dir / filename, content.rstrip() + "\n")

    def _write_if_missing(self, path: Path, content: str) -> None:
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    def relative_path(self, path: Path | str) -> str:
        candidate = Path(path)
        try:
            return str(candidate.resolve().relative_to(self.root))
        except Exception:
            return str(candidate)

    def _override_or_primary_path(self, primary: Path, override: Path) -> Path:
        return override if override.exists() else primary

    def _save_target(self, primary: Path, override: Path) -> Path:
        target = override if override.exists() else primary
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _save_local_only_target(self, override: Path) -> Path:
        override.parent.mkdir(parents=True, exist_ok=True)
        return override

    def load_user_profile(self) -> dict[str, Any]:
        self.ensure()
        payload = _read_yaml(self.user_profile_path) or {}
        return payload if isinstance(payload, dict) else {}

    def user_profile_surface(self) -> dict[str, Any]:
        self.ensure()
        advanced_paths = [
            self.local_profile_path,
            self.local_facts_path,
            self.local_answer_memory_path,
            self.local_cv_path,
            self.local_candidate_dossier_path,
        ]
        active_advanced_paths = [self.relative_path(path) for path in advanced_paths if path.exists()]
        has_user_profile = self.user_profile_path.exists()
        mode = "sample_mode"
        if has_user_profile:
            mode = "local_user_profile"
        elif active_advanced_paths:
            mode = "advanced_local_overrides"
        return {
            "mode": mode,
            "configured": mode != "sample_mode",
            "has_user_profile": has_user_profile,
            "local_path": self.relative_path(self.user_profile_path),
            "local_template_path": self.relative_path(self.user_profile_template_path),
            "public_template_path": self.relative_path(self.public_user_profile_template_path),
            "active_advanced_paths": active_advanced_paths,
        }

    def _user_profile_resume_path(self) -> Path | None:
        payload = self.load_user_profile()
        resume = payload.get("resume") if isinstance(payload.get("resume"), dict) else {}
        return _resolve_workspace_relative_path(self.root, resume.get("markdown_path"))

    def _user_profile_dossier_path(self) -> Path | None:
        payload = self.load_user_profile()
        resume = payload.get("resume") if isinstance(payload.get("resume"), dict) else {}
        return _resolve_workspace_relative_path(self.root, resume.get("dossier_path"))

    def load_profile(self) -> WorkspaceProfile:
        self.ensure()
        payload = _deep_merge_dicts(DEFAULT_PROFILE, _read_yaml(self.profile_path) or {})
        user_profile_payload = self.load_user_profile()
        if user_profile_payload:
            payload = _deep_merge_dicts(payload, _build_user_profile_patch(user_profile_payload))
        override_payload = _read_yaml(self.local_profile_path) or {}
        if isinstance(override_payload, dict):
            payload = _deep_merge_dicts(payload, override_payload)
        return WorkspaceProfile.model_validate(payload)

    def save_profile(self, profile: WorkspaceProfile) -> None:
        self.ensure()
        self._save_local_only_target(self.local_profile_path).write_text(
            _yaml_dump(profile.model_dump(mode="json")),
            encoding="utf-8",
        )

    def load_portals(self) -> PortalsConfig:
        self.ensure()
        return PortalsConfig.model_validate(_read_yaml(self.portals_path) or {})

    def save_portals(self, portals: PortalsConfig) -> None:
        self.ensure()
        self.portals_path.write_text(_yaml_dump(portals.model_dump(mode="json")), encoding="utf-8")

    def load_facts(self) -> list[FileFact]:
        self.ensure()
        if self.local_facts_path.exists():
            payload = _read_yaml(self.local_facts_path) or {}
            rows = payload.get("facts", []) if isinstance(payload, dict) else payload
            return [FileFact.model_validate(row) for row in rows or []]
        user_profile_payload = self.load_user_profile()
        if user_profile_payload:
            rows = _build_user_profile_facts(user_profile_payload)
            return [FileFact.model_validate(row) for row in rows or []]
        payload = _read_yaml(self.facts_path) or {}
        rows = payload.get("facts", []) if isinstance(payload, dict) else payload
        return [FileFact.model_validate(row) for row in rows or []]

    def save_facts(self, facts: list[FileFact]) -> None:
        self.ensure()
        self._save_local_only_target(self.local_facts_path).write_text(
            _yaml_dump({"facts": [fact.model_dump(mode="json") for fact in facts]}),
            encoding="utf-8",
        )

    def load_answer_memory(self) -> list[AnswerMemoryEntry]:
        self.ensure()
        if self.local_answer_memory_path.exists():
            payload = _read_yaml(self.local_answer_memory_path) or {}
            rows = payload.get("answers", []) if isinstance(payload, dict) else payload
            return [AnswerMemoryEntry.model_validate(row) for row in rows or []]
        payload = _read_yaml(self.answer_memory_path) or {}
        rows = payload.get("answers", []) if isinstance(payload, dict) else payload
        merged_rows: list[dict[str, Any]] = []
        seen_signatures: set[tuple[str, str, str, bool]] = set()

        def _append_row(row: dict[str, Any]) -> None:
            canonical = str(row.get("canonical_question") or "").strip()
            if not canonical:
                return
            constraints = row.get("context_constraints") if isinstance(row.get("context_constraints"), dict) else {}
            signature = (
                canonical,
                json.dumps(constraints, sort_keys=True),
                str(row.get("answer_text") or "").strip(),
                bool(row.get("approved", False)),
            )
            if signature in seen_signatures:
                return
            seen_signatures.add(signature)
            merged_rows.append(dict(row))

        for item in rows or []:
            if isinstance(item, dict):
                _append_row(item)
        for row in _build_user_profile_answer_memory(self.load_user_profile()):
            if isinstance(row, dict):
                _append_row(row)
        return [AnswerMemoryEntry.model_validate(row) for row in merged_rows]

    def save_answer_memory(self, answers: list[AnswerMemoryEntry]) -> None:
        self.ensure()
        self._save_local_only_target(self.local_answer_memory_path).write_text(
            _yaml_dump({"answers": [item.model_dump(mode="json") for item in answers]}),
            encoding="utf-8",
        )

    def load_cv(self) -> str:
        self.ensure()
        if self.local_cv_path.exists():
            return self.local_cv_path.read_text(encoding="utf-8")
        user_profile_resume = self._user_profile_resume_path()
        if user_profile_resume is not None and user_profile_resume.exists():
            return user_profile_resume.read_text(encoding="utf-8")
        return self.cv_path.read_text(encoding="utf-8")

    def save_cv(self, body: str) -> None:
        self.ensure()
        self._save_local_only_target(self.local_cv_path).write_text(body.rstrip() + "\n", encoding="utf-8")

    def load_candidate_dossier(self) -> str | None:
        self.ensure()
        target = self.local_candidate_dossier_path if self.local_candidate_dossier_path.exists() else self.candidate_dossier_path
        if not target.exists():
            user_profile_dossier = self._user_profile_dossier_path()
            if user_profile_dossier is not None and user_profile_dossier.exists():
                target = user_profile_dossier
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    def save_candidate_dossier(self, body: str) -> None:
        self.ensure()
        self._save_local_only_target(self.local_candidate_dossier_path).write_text(
            body.rstrip() + "\n",
            encoding="utf-8",
        )

    def load_live_state(self) -> LiveRunState:
        self.ensure()
        if not self.live_state_path.exists():
            return LiveRunState()
        try:
            return LiveRunState.model_validate(json.loads(self.live_state_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError, OSError):
            return LiveRunState()

    def save_live_state(self, state: LiveRunState) -> Path:
        self.ensure()
        payload = state.model_copy(update={"updated_at": utcnow_iso()})
        self.live_state_path.write_text(json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.live_state_path

    def load_chatgpt_drafting_status(self) -> dict[str, Any]:
        self.ensure()
        if not self.chatgpt_drafting_status_path.exists():
            return {}
        try:
            payload = json.loads(self.chatgpt_drafting_status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_chatgpt_drafting_status(self, payload: dict[str, Any]) -> Path:
        self.ensure()
        body = dict(payload or {})
        body["updated_at"] = utcnow_iso()
        self.chatgpt_drafting_status_path.write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.chatgpt_drafting_status_path

    def load_handled_jobs(self) -> dict[str, Any]:
        self.ensure()
        if not self.handled_jobs_path.exists():
            return {"job_ids": [], "urls": [], "pairs": [], "duplicate_clusters": []}
        try:
            payload = json.loads(self.handled_jobs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"job_ids": [], "urls": [], "pairs": [], "duplicate_clusters": []}
        if not isinstance(payload, dict):
            return {"job_ids": [], "urls": [], "pairs": [], "duplicate_clusters": []}
        return {
            "job_ids": list(payload.get("job_ids") or []),
            "urls": list(payload.get("urls") or []),
            "pairs": list(payload.get("pairs") or []),
            "duplicate_clusters": list(payload.get("duplicate_clusters") or []),
            "updated_at": payload.get("updated_at"),
        }

    def save_handled_jobs(self, payload: dict[str, Any]) -> Path:
        self.ensure()
        body = {
            "job_ids": list(payload.get("job_ids") or []),
            "urls": list(payload.get("urls") or []),
            "pairs": list(payload.get("pairs") or []),
            "duplicate_clusters": list(payload.get("duplicate_clusters") or []),
            "updated_at": utcnow_iso(),
        }
        self.handled_jobs_path.write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.handled_jobs_path

    def append_live_event(self, event: LiveRunEvent) -> Path:
        self.ensure()
        with self.live_events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")
        return self.live_events_path

    def load_board_discovery_state(self) -> BoardDiscoveryState:
        self.ensure()
        if not self.board_discovery_path.exists():
            return BoardDiscoveryState()
        try:
            return BoardDiscoveryState.model_validate(json.loads(self.board_discovery_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError, OSError):
            return BoardDiscoveryState()

    def save_board_discovery_state(self, state: BoardDiscoveryState) -> Path:
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self.board_discovery_path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.board_discovery_path

    def load_live_events(self, *, limit: int | None = None, run_id: str | None = None) -> list[LiveRunEvent]:
        self.ensure()
        if not self.live_events_path.exists():
            return []
        events: list[LiveRunEvent] = []
        for line in self.live_events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if run_id is not None and str(payload.get("run_id") or "") != run_id:
                    continue
                events.append(LiveRunEvent.model_validate(payload))
            except (json.JSONDecodeError, ValidationError, OSError):
                continue
        if limit is not None:
            events = events[-limit:]
        return events

    def live_run_dir(self, run_id: str) -> Path:
        safe_run_id = slugify(run_id) or 'run'
        return self.live_runs_dir / safe_run_id

    _TRACE_SENSITIVE_KEYS = frozenset({
        "email", "phone", "phone_number", "address", "ssn",
        "social_security", "date_of_birth", "dob", "citizenship",
        "ethnicity", "race", "gender", "disability", "veteran_status",
        "api_key", "password", "secret", "token",
        "prompt", "system_prompt", "request_payload", "raw_response", "response",
        "response_content", "parsed_output", "answer", "answer_text",
        "prompt_text", "html", "dom", "dom_snapshot",
    })

    @classmethod
    def _redact_trace_payload(cls, payload: Any) -> Any:
        """Redact PII-sensitive fields from trace payloads before persisting."""
        if isinstance(payload, dict):
            redacted = {}
            for key, value in payload.items():
                lower_key = str(key).lower().replace("-", "_").replace(" ", "_")
                if lower_key in cls._TRACE_SENSITIVE_KEYS:
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = cls._redact_trace_payload(value)
            return redacted
        if isinstance(payload, list):
            return [cls._redact_trace_payload(item) for item in payload]
        return payload

    def write_live_trace(self, run_id: str, *, category: str, name: str, payload: Any) -> str:
        self.ensure()
        category_dir = self.live_run_dir(run_id) / (slugify(category) or 'trace')
        category_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{slugify(name) or 'trace'}.json"
        trace_path = category_dir / filename
        sanitized = self._redact_trace_payload(payload)
        trace_path.write_text(json.dumps(sanitized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.relative_path(trace_path)

    def load_live_trace(self, trace_ref: str) -> dict[str, Any]:
        self.ensure()
        candidate = (self.root / str(trace_ref or '')).resolve()
        live_root = self.live_dir.resolve()
        if live_root not in {candidate, *candidate.parents}:
            raise ValueError(f"Trace is outside the live run directory: {trace_ref}")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(trace_ref)
        return json.loads(candidate.read_text(encoding="utf-8"))

    def job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def save_job(self, job: InboxJob) -> Path:
        self.ensure()
        path = self.job_path(job.job_id)
        path.write_text(json.dumps(job.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def load_job(self, job_id: str) -> InboxJob | None:
        path = self.job_path(job_id)
        if not path.exists():
            return None
        try:
            return InboxJob.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError, OSError):
            return None

    def delete_job(self, job_id: str) -> bool:
        """Delete the job JSON file from data/jobs/. Returns True if file existed."""
        path = self.job_path(job_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def remove_from_inbox(self, job_ids: set[str]) -> int:
        """Remove jobs with given IDs from inbox.tsv. Returns count removed."""
        if not job_ids:
            return 0
        rows = self._read_tsv(self.inbox_path)
        before = len(rows)
        rows = [row for row in rows if row.get("job_id", "") not in job_ids]
        self._write_tsv(self.inbox_path, INBOX_COLUMNS, rows)
        return before - len(rows)

    def evaluation_path(self, job_id: str) -> Path:
        return self.evaluations_dir / f"{job_id}.json"

    def save_evaluation(self, evaluation: EvaluationResult) -> Path:
        self.ensure()
        path = self.evaluation_path(evaluation.job_id)
        path.write_text(json.dumps(evaluation.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def load_evaluation(self, job_id: str) -> EvaluationResult | None:
        path = self.evaluation_path(job_id)
        if not path.exists():
            return None
        try:
            return EvaluationResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError, OSError):
            return None

    def submission_path(self, application_id: str) -> Path:
        return self.submissions_dir / f"{application_id}.json"

    def save_submission(self, submission: SubmissionRecord) -> Path:
        self.ensure()
        payload = submission.model_copy(update={"updated_at": utcnow_iso()})
        path = self.submission_path(payload.application_id)
        path.write_text(json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def load_submission(self, application_id: str) -> SubmissionRecord | None:
        path = self.submission_path(application_id)
        if not path.exists():
            return None
        try:
            return SubmissionRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError, OSError):
            return None

    def load_submissions(self) -> list[SubmissionRecord]:
        self.ensure()
        entries: list[SubmissionRecord] = []
        for path in sorted(self.submissions_dir.glob("*.json")):
            try:
                entries.append(SubmissionRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, ValidationError, OSError):
                continue
        entries.sort(key=lambda item: item.updated_at, reverse=True)
        return entries

    def upsert_submission(self, submission: SubmissionRecord) -> SubmissionRecord:
        existing = self.load_submission(submission.application_id)
        payload = submission
        if existing is not None and not submission.created_at:
            payload = submission.model_copy(update={"created_at": existing.created_at})
        self.save_submission(payload)
        return payload

    def find_submission(self, target: str) -> SubmissionRecord | None:
        for item in self.load_submissions():
            if target in {item.application_id, item.job_id}:
                return item
        return None

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def save_run(self, run: RunRecord) -> Path:
        self.ensure()
        path = self.run_path(run.run_id)
        path.write_text(json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def load_run(self, run_id: str) -> RunRecord | None:
        path = self.run_path(run_id)
        if not path.exists():
            return None
        try:
            return RunRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError, OSError):
            return None

    def load_runs(self) -> list[RunRecord]:
        self.ensure()
        runs: list[RunRecord] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                runs.append(RunRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, ValidationError, OSError):
                continue
        runs.sort(key=lambda item: item.started_at, reverse=True)
        return runs

    def _read_tsv(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            return [{str(key): str(value or "") for key, value in row.items()} for row in reader]

    def _write_tsv(self, path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name, "") for name in fieldnames})

    def load_inbox(self) -> list[InboxJob]:
        rows = self._read_tsv(self.inbox_path)
        jobs: list[InboxJob] = []
        for row in rows:
            stored = self.load_job(row.get("job_id", ""))
            payload = stored.model_dump(mode="json") if stored is not None else {"notes": {}, "description": "", "screening": None}
            payload.update({
                "job_id": row.get("job_id", payload.get("job_id", "")),
                "company": row.get("company", payload.get("company", "")),
                "title": row.get("title", payload.get("title", "")),
                "source": row.get("source", payload.get("source", "")),
                "source_kind": row.get("source_kind", payload.get("source_kind", "")),
                "source_job_id": row.get("source_job_id", payload.get("source_job_id", "")),
                "url": row.get("url", payload.get("url", "")),
                "apply_url": row.get("apply_url") or payload.get("apply_url"),
                "location": row.get("location") or payload.get("location"),
                "posted_at": row.get("posted_at") or payload.get("posted_at"),
                "discovered_at": row.get("discovered_at") or payload.get("discovered_at") or utcnow_iso(),
                "ats_family": row.get("ats_family") or payload.get("ats_family") or row.get("board_family") or payload.get("board_family") or "unknown",
                "ats_preview_supported": str(row.get("ats_preview_supported") or payload.get("ats_preview_supported") or "").strip().lower() in {"1", "true", "yes"},
                "hard_reject_reason": row.get("hard_reject_reason") or payload.get("hard_reject_reason"),
                "auth_reject_reason": row.get("auth_reject_reason") or payload.get("auth_reject_reason"),
                "login_wall_detected": str(row.get("login_wall_detected") or payload.get("login_wall_detected") or "").strip().lower() in {"1", "true", "yes"},
                "rehearsal_eligible": str(row.get("rehearsal_eligible") or payload.get("rehearsal_eligible") or "").strip().lower() in {"1", "true", "yes"},
                "rehearsal_rank": float(row.get("rehearsal_rank") or payload.get("rehearsal_rank") or 0.0),
                "discovery_method": row.get("discovery_method") or payload.get("discovery_method"),
                "workflow_state": row.get("workflow_state") or payload.get("workflow_state") or "pending",
                "board_family": row.get("board_family") or payload.get("board_family") or "unknown",
                "automation_tier": row.get("automation_tier") or payload.get("automation_tier") or "unsupported_high_friction",
                "job_identity_key": row.get("job_identity_key") or payload.get("job_identity_key") or row.get("job_id", ""),
                "duplicate_cluster_key": row.get("duplicate_cluster_key") or payload.get("duplicate_cluster_key") or row.get("job_id", ""),
            })
            payload["screening"] = payload.get("screening") or (_screening_from_row(row).model_dump(mode="json") if _screening_from_row(row) is not None else None)
            jobs.append(InboxJob.model_validate(payload))
        return jobs

    def save_inbox(self, jobs: list[InboxJob]) -> None:
        rows = [_inbox_row(job) for job in jobs]
        self._write_tsv(self.inbox_path, INBOX_COLUMNS, rows)

    def upsert_inbox_jobs(self, jobs: list[InboxJob]) -> tuple[int, int]:
        with type(self)._INBOX_LOCK:
            existing = {job.job_id: job for job in self.load_inbox()}
            created = 0
            updated = 0
            for job in jobs:
                if job.job_id in existing:
                    current = existing[job.job_id]
                    existing[job.job_id] = current.model_copy(
                        update={
                            "company": job.company,
                            "title": job.title,
                            "source": job.source,
                            "source_kind": job.source_kind,
                            "source_job_id": job.source_job_id,
                            "url": job.url,
                            "apply_url": job.apply_url,
                            "location": job.location,
                            "posted_at": job.posted_at,
                            "description": job.description or current.description,
                            "notes": job.notes or current.notes,
                            "screening": job.screening or current.screening,
                            "workflow_state": job.workflow_state or current.workflow_state,
                            "ats_family": job.ats_family or current.ats_family,
                            "ats_preview_supported": job.ats_preview_supported,
                            "hard_reject_reason": job.hard_reject_reason or current.hard_reject_reason,
                            "auth_reject_reason": job.auth_reject_reason or current.auth_reject_reason,
                            "login_wall_detected": job.login_wall_detected,
                            "rehearsal_eligible": job.rehearsal_eligible,
                            "rehearsal_rank": job.rehearsal_rank,
                            "discovery_method": job.discovery_method or current.discovery_method,
                            "board_family": job.board_family,
                            "automation_tier": job.automation_tier,
                            "job_identity_key": job.job_identity_key,
                            "duplicate_cluster_key": job.duplicate_cluster_key,
                        }
                    )
                    updated += 1
                else:
                    existing[job.job_id] = job
                    created += 1
            ordered = sorted(existing.values(), key=lambda item: item.discovered_at, reverse=True)
            self.save_inbox(ordered)
            for job in ordered:
                self.save_job(job)
            return created, updated

    def update_inbox_state(self, job_id: str, workflow_state: str) -> None:
        with type(self)._INBOX_LOCK:
            jobs = self.load_inbox()
            changed = False
            for job in jobs:
                if job.job_id == job_id:
                    job.workflow_state = workflow_state
                    self.save_job(job)
                    changed = True
                    break
            if changed:
                self.save_inbox(jobs)

    def load_scan_history(self) -> list[dict[str, str]]:
        return self._read_tsv(self.scan_history_path)

    def append_scan_history(self, jobs: list[InboxJob]) -> None:
        history = self.load_scan_history()
        seen = {(row.get("job_id", ""), row.get("url", "")) for row in history}
        for job in jobs:
            key = (job.job_id, job.url)
            if key in seen:
                continue
            history.append(
                {
                    "seen_at": date.today().isoformat(),
                    "job_id": job.job_id,
                    "url": job.url,
                    "company": job.company,
                    "title": job.title,
                    "source": job.source,
                    "duplicate_cluster_key": job.duplicate_cluster_key,
                }
            )
        self._write_tsv(self.scan_history_path, SCAN_HISTORY_COLUMNS, history)

    def load_applications(self) -> list[ApplicationEntry]:
        if not self.applications_path.exists():
            return []
        rows = [line for line in self.applications_path.read_text(encoding="utf-8").splitlines() if line.strip().startswith("|")]
        if len(rows) < 2:
            return []
        headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
        entries: list[ApplicationEntry] = []
        for line in rows[2:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) != len(headers):
                continue
            payload = dict(zip(headers, cells, strict=False))
            entries.append(
                ApplicationEntry(
                    id=payload.get("#", ""),
                    job_id=payload.get("Job ID", ""),
                    date=payload.get("Date", ""),
                    company=payload.get("Company", ""),
                    role=payload.get("Role", ""),
                    score=float(payload.get("Score", "0") or 0.0),
                    grade=payload.get("Grade", "F") or "F",
                    status=payload.get("Status", "Evaluated") or "Evaluated",
                    pdf=str(payload.get("PDF", "")).strip().lower() in {"yes", "true", "1", "pdf"},
                    report=payload.get("Report", "") or "",
                    url=payload.get("URL", "") or "",
                    source=payload.get("Source", "") or "",
                    notes=payload.get("Notes", "") or None,
                )
            )
        return entries

    def save_applications(self, entries: list[ApplicationEntry]) -> None:
        lines = [
            "| " + " | ".join(APPLICATION_HEADERS) + " |",
            "| " + " | ".join("---" for _ in APPLICATION_HEADERS) + " |",
        ]
        for entry in entries:
            row = [
                entry.id,
                entry.job_id,
                entry.date,
                entry.company,
                entry.role,
                f"{entry.score:.2f}",
                entry.grade,
                entry.status,
                "yes" if entry.pdf else "no",
                entry.report,
                entry.url,
                entry.source,
                entry.notes or "",
            ]
            lines.append("| " + " | ".join(row) + " |")
        self.applications_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def next_application_id(self) -> str:
        highest = 0
        for entry in self.load_applications():
            try:
                highest = max(highest, int(entry.id))
            except ValueError:
                continue
        return f"{highest + 1:03d}"

    def upsert_application(self, entry: ApplicationEntry) -> ApplicationEntry:
        entries = self.load_applications()
        replaced = False
        for index, current in enumerate(entries):
            if current.job_id == entry.job_id or (entry.url and current.url == entry.url):
                entries[index] = entry
                replaced = True
                break
        if not replaced:
            entries.append(entry)
        entries.sort(key=lambda item: (item.date, item.id), reverse=True)
        self.save_applications(entries)
        return entry

    def find_application(self, target: str) -> ApplicationEntry | None:
        for entry in self.load_applications():
            if target in {entry.id, entry.job_id, entry.report, entry.url}:
                return entry
        return None

    def report_path_for(self, display_id: str, company: str, on_date: str | None = None) -> Path:
        stamp = on_date or date.today().isoformat()
        return self.reports_dir / f"{display_id}-{slugify(company)}-{stamp}.md"

    def resume_html_path_for(self, display_id: str, company: str, on_date: str | None = None) -> Path:
        stamp = on_date or date.today().isoformat()
        return self.output_dir / f"cv-{display_id}-{slugify(company)}-{stamp}.html"

    def resume_pdf_path_for(self, display_id: str, company: str, on_date: str | None = None) -> Path:
        stamp = on_date or date.today().isoformat()
        return self.output_dir / f"cv-{display_id}-{slugify(company)}-{stamp}.pdf"

    def chatgpt_download_dir_for(self, application_id: str) -> Path:
        safe_application_id = slugify(application_id) or "application"
        return self.runtime_dir / "chatgpt-downloads" / safe_application_id
