from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from findmyjob.filefirst.models import InboxJob, WorkspaceProfile
from findmyjob.filefirst.source_targets import resolve_targets
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.adapters.ashby import AshbyAdapter
from findmyjob.sources.adapters.greenhouse import GreenhouseAdapter
from findmyjob.sources.adapters.lever import LeverAdapter
from findmyjob.sources.classification import classify_job
from findmyjob.sources.contracts import DiscoveryQuery

_PRODUCTION_SOURCES = {"greenhouse", "lever", "ashby"}


@dataclass(slots=True)
class DiscoverySeenState:
    job_ids: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    pairs: set[tuple[str, str]] = field(default_factory=set)
    duplicate_clusters: set[str] = field(default_factory=set)


def _ordered_unique_casefold(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def _effective_title_keywords(profile: WorkspaceProfile) -> list[str]:
    keywords = list(profile.targets.title_keywords or [])
    try:
        from findmyjob.personal.onboarding import DEFAULT_PRESET_DEFINITIONS

        for preset_name in profile.candidate.target_roles or []:
            definition = DEFAULT_PRESET_DEFINITIONS.get(str(preset_name or "").strip())
            if definition is None:
                continue
            keywords.extend(list(definition.get("title_keywords") or []))
    except Exception:
        # Discovery should still work even if preset metadata is unavailable.
        pass
    return _ordered_unique_casefold(keywords)


def _excluded_title(title: str, excluded_keywords: list[str]) -> bool:
    lowered = title.casefold()
    return any(keyword.casefold() in lowered for keyword in excluded_keywords if keyword.strip())


def _blocked_company(company: str, blocked_companies: list[str]) -> bool:
    lowered = company.casefold().strip()
    return any(blocked.casefold().strip() in lowered or lowered in blocked.casefold().strip() for blocked in blocked_companies if blocked.strip())


def _normalize_discovery_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    normalized_path = parsed.path.rstrip("/") or parsed.path
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), normalized_path, "", ""))


def _company_role_key(company: str | None, role: str | None) -> tuple[str, str] | None:
    company_key = str(company or "").casefold().strip()
    role_key = str(role or "").casefold().strip()
    if not company_key or not role_key:
        return None
    return (company_key, role_key)


def build_discovery_seen_state(ws: FileWorkspace) -> DiscoverySeenState:
    state = DiscoverySeenState()
    handled = ws.load_handled_jobs()
    for job_id in handled.get("job_ids") or []:
        normalized_job_id = str(job_id or "").strip()
        if normalized_job_id:
            state.job_ids.add(normalized_job_id)
    for url in handled.get("urls") or []:
        normalized = _normalize_discovery_url(url)
        if normalized:
            state.urls.add(normalized)
    for row in handled.get("pairs") or []:
        if not isinstance(row, dict):
            continue
        key = _company_role_key(row.get("company"), row.get("role"))
        if key is not None:
            state.pairs.add(key)
    for cluster in handled.get("duplicate_clusters") or []:
        normalized_cluster = str(cluster or "").strip()
        if normalized_cluster:
            state.duplicate_clusters.add(normalized_cluster)
    for row in ws.load_scan_history():
        state.job_ids.add(str(row.get("job_id") or "").strip())
        normalized = _normalize_discovery_url(row.get("url"))
        if normalized:
            state.urls.add(normalized)
    inbox_jobs = ws.load_inbox()
    inbox_by_id = {job.job_id: job for job in inbox_jobs}
    for job in inbox_jobs:
        if job.job_id:
            state.job_ids.add(job.job_id)
        for candidate in (job.url, job.apply_url):
            normalized = _normalize_discovery_url(candidate)
            if normalized:
                state.urls.add(normalized)
        key = _company_role_key(job.company, job.title)
        if key is not None:
            state.pairs.add(key)
        cluster = str(job.duplicate_cluster_key or "").strip()
        if cluster:
            state.duplicate_clusters.add(cluster)
    for entry in ws.load_applications():
        if entry.job_id:
            state.job_ids.add(entry.job_id)
        normalized = _normalize_discovery_url(entry.url)
        if normalized:
            state.urls.add(normalized)
        key = _company_role_key(entry.company, entry.role)
        if key is not None:
            state.pairs.add(key)
        linked_job = inbox_by_id.get(entry.job_id) or ws.load_job(entry.job_id)
        if linked_job is not None:
            cluster = str(linked_job.duplicate_cluster_key or "").strip()
            if cluster:
                state.duplicate_clusters.add(cluster)
    return state


def discovery_seen_match(job: InboxJob, state: DiscoverySeenState) -> bool:
    key = _company_role_key(job.company, job.title)
    return bool(
        (job.job_id and job.job_id in state.job_ids)
        or (_normalize_discovery_url(job.url) and _normalize_discovery_url(job.url) in state.urls)
        or (_normalize_discovery_url(job.apply_url) and _normalize_discovery_url(job.apply_url) in state.urls)
        or (key is not None and key in state.pairs)
        or (str(job.duplicate_cluster_key or "").strip() and str(job.duplicate_cluster_key or "").strip() in state.duplicate_clusters)
    )


def remember_discovered_job(job: InboxJob, state: DiscoverySeenState) -> None:
    if job.job_id:
        state.job_ids.add(job.job_id)
    for candidate in (job.url, job.apply_url):
        normalized = _normalize_discovery_url(candidate)
        if normalized:
            state.urls.add(normalized)
    key = _company_role_key(job.company, job.title)
    if key is not None:
        state.pairs.add(key)
    cluster = str(job.duplicate_cluster_key or "").strip()
    if cluster:
        state.duplicate_clusters.add(cluster)


def build_discovery_query(profile: WorkspaceProfile) -> DiscoveryQuery:
    targets = profile.targets
    return DiscoveryQuery(
        title_keywords=_effective_title_keywords(profile),
        locations=list(targets.locations),
        countries=list(targets.countries),
        regions=list(targets.regions),
        cities=list(targets.cities),
        employment_types=list(targets.employment_types),
        posted_within_days=targets.posted_within_days,
        remote_only=targets.remote_only,
    )


def _adapter_for(source_name: str, boards: list[str]):
    if source_name == "greenhouse":
        return GreenhouseAdapter(boards)
    if source_name == "lever":
        return LeverAdapter(boards)
    if source_name == "ashby":
        return AshbyAdapter(boards)
    raise ValueError(f"Unsupported source for file-first production scan: {source_name}")


def _posting_to_inbox(posting) -> InboxJob:
    classification = classify_job(
        source_kind=posting.source_kind,
        apply_url=str(posting.apply_url or posting.posting_url),
        posting_url=str(posting.posting_url),
        source_adapter=posting.source,
        notes=dict(posting.notes or {}),
    )
    return InboxJob(
        job_id=posting.job_identity_key,
        company=posting.company_name,
        company_key=posting.company_key,
        title=posting.title,
        source=posting.source,
        source_kind=posting.source_kind,
        source_job_id=posting.source_job_id,
        url=str(posting.posting_url),
        apply_url=str(posting.apply_url) if posting.apply_url else None,
        location=posting.location_raw or posting.location_normalized,
        posted_at=posting.posted_at.isoformat() if posting.posted_at else None,
        description=posting.description,
        workflow_state="pending",
        board_family=classification.board_family.value,
        automation_tier=classification.automation_tier.value,
        job_identity_key=posting.job_identity_key,
        duplicate_cluster_key=posting.duplicate_cluster_key,
        notes=dict(posting.notes or {}),
    )


async def scan_workspace(
    workspace: Path | FileWorkspace,
    *,
    source: str | None = None,
    board: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    profile = ws.load_profile()
    query = build_discovery_query(profile)
    targets = resolve_targets(ws, source=source, board=board)
    if not targets:
        return {"targets": {}, "discovered": 0, "new_jobs": 0, "updated_jobs": 0, "duplicates": 0}

    seen_state = build_discovery_seen_state(ws)

    discovered_count = 0
    duplicates = 0
    accepted: list[InboxJob] = []
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for source_name, boards in targets.items():
            for board_name in boards:
                adapter = _adapter_for(source_name, [board_name])
                try:
                    postings = await adapter.discover(client, query)
                except Exception as exc:
                    errors.append(f"{source_name}/{board_name}: {exc}")
                    continue
                for posting, _payload in postings:
                    discovered_count += 1
                    if _excluded_title(posting.title, profile.targets.excluded_keywords):
                        duplicates += 1
                        continue
                    job = _posting_to_inbox(posting)
                    if _blocked_company(job.company, profile.targets.blocked_companies):
                        duplicates += 1
                        continue
                    if discovery_seen_match(job, seen_state):
                        duplicates += 1
                        continue
                    ws.save_job(job)
                    accepted.append(job)
                    remember_discovered_job(job, seen_state)
                    if limit is not None and len(accepted) >= limit:
                        break
                if limit is not None and len(accepted) >= limit:
                    break
            if limit is not None and len(accepted) >= limit:
                break

    created, updated = ws.upsert_inbox_jobs(accepted)
    ws.append_scan_history(accepted)
    return {
        "targets": targets,
        "discovered": discovered_count,
        "new_jobs": created,
        "updated_jobs": updated,
        "duplicates": duplicates,
        "errors": errors,
        "saved_job_ids": [job.job_id for job in accepted],
    }
