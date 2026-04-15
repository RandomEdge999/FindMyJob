from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from findmyjob.apply.browser import analyze_dom_snapshot
from findmyjob.filefirst.discovery import (
    _blocked_company,
    _excluded_title,
    _posting_to_inbox,
    build_discovery_query,
    build_discovery_seen_state,
    discovery_seen_match,
    remember_discovered_job,
)
from findmyjob.filefirst.models import BoardDiscoveryState, InboxJob, SourceDiscoveryMetrics, utcnow_iso
from findmyjob.filefirst.source_targets import (
    SOURCE_ORDER as LIVE_SOURCE_ORDER,
    active_sources as resolved_active_sources,
    builtin_targets,
    configured_targets as configured_source_targets,
    extract_board_token as _extract_board_token,
    extract_board_tokens as _extract_board_tokens,
    fallback_targets,
    filter_targets,
    is_generic_source_host as _is_generic_source_host,
    normalize_board_token as _normalize_board_token,
    normalize_domain as _normalize_domain,
    ordered_unique as _ordered_unique,
    persisted_targets as persisted_source_targets,
    seed_domains as discovery_seed_domains,
    seed_urls as discovery_seed_urls,
)
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.sources.adapters.ashby import AshbyAdapter
from findmyjob.sources.adapters.greenhouse import GreenhouseAdapter
from findmyjob.sources.adapters.lever import LeverAdapter
from findmyjob.sources.classification import classify_job
from findmyjob.sources.greenhouse_scale import career_like_links, parse_links, parse_sitemap_urls
from findmyjob.sources.normalizer import build_normalized_job

SUPPORTED_PREVIEW_FAMILIES = {"greenhouse", "lever", "ashby"}
UNSUPPORTED_SKIP_FAMILIES = {
    "workday",
    "icims",
    "taleo",
    "successfactors",
    "smartrecruiters",
    "jobvite",
    "bamboohr",
    "unknown",
}
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"\s+")
_JOB_PATH_PATTERN = re.compile(r"/jobs?/([^/?#]+)", re.IGNORECASE)

_AUTH_REJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("us_citizen_only", re.compile(r"\b(?:u\.?s\.?\s+citizen(?:ship)?(?:s)?|american citizen(?:s)?)\b.{0,40}\b(?:required|only|must)\b", re.IGNORECASE)),
    ("us_person_only", re.compile(r"\bu\.?s\.?\s+person(?:s)?\b.{0,30}\b(?:required|only|must)\b", re.IGNORECASE)),
    ("permanent_resident_only", re.compile(r"\b(?:permanent resident|green card holder)\b.{0,30}\b(?:required|only|must)\b", re.IGNORECASE)),
    ("no_sponsorship", re.compile(r"\b(?:no|not|unable|cannot|won't|will not)\b.{0,20}\b(?:sponsor|sponsorship|visa sponsorship)\b", re.IGNORECASE)),
    ("no_opt_cpt", re.compile(r"\b(?:no|not|unable|cannot|won't|will not)\b.{0,20}\b(?:opt|cpt|stem opt|h-?1b|h1b)\b", re.IGNORECASE)),
    ("security_clearance_required", re.compile(r"\b(?:security clearance|secret clearance|top secret|ts\/sci|clearance required)\b", re.IGNORECASE)),
)

_ENTRY_HINTS = ("new grad", "entry level", "early career", "software engineer i", "engineer i", "junior", "associate")
_SENIOR_HINTS = ("senior", "staff", "principal", "manager", "director", "lead", "architect")
_DEAD_PAGE_HINTS = (
    "browse all open jobs",
    "view all open jobs",
    "see all open jobs",
    "browse open jobs",
    "open roles",
    "open positions",
    "all open roles",
)


@dataclass(slots=True)
class SeedDiscovery:
    board_targets: dict[str, set[str]] = field(default_factory=lambda: {name: set() for name in LIVE_SOURCE_ORDER})
    source_domains: dict[str, set[str]] = field(default_factory=lambda: {name: set() for name in LIVE_SOURCE_ORDER})
    unsupported_urls: list[dict[str, str]] = field(default_factory=list)
    crawled_pages: int = 0
    errors: list[str] = field(default_factory=list)


def _adapter_for(source_name: str, boards: list[str]):
    if source_name == "greenhouse":
        return GreenhouseAdapter(boards)
    if source_name == "lever":
        return LeverAdapter(boards)
    if source_name == "ashby":
        return AshbyAdapter(boards)
    raise ValueError(f"Unsupported live-market source: {source_name}")


def _emit_progress(progress_callback: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(dict(payload))
    except Exception:
        return


def _seed_urls(ws: FileWorkspace) -> list[str]:
    return discovery_seed_urls(ws)


def _seed_domains(ws: FileWorkspace) -> list[str]:
    return discovery_seed_domains(ws)


def _builtin_targets() -> dict[str, list[str]]:
    return builtin_targets()


def _configured_targets(ws: FileWorkspace) -> dict[str, list[str]]:
    return configured_source_targets(ws)


def _persisted_targets(ws: FileWorkspace) -> dict[str, list[str]]:
    return persisted_source_targets(ws)


def _active_sources(ws: FileWorkspace) -> list[str]:
    return resolved_active_sources(ws)


def _filter_targets(targets: dict[str, list[str]], active_sources: list[str]) -> dict[str, list[str]]:
    return filter_targets(targets, active_sources)


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, timeout=20.0)
    response.raise_for_status()
    content_type = str(response.headers.get("content-type") or "").lower()
    if not content_type:
        return response.text
    if any(marker in content_type for marker in ("text/html", "application/xhtml+xml", "application/xml", "text/xml", "text/plain")):
        return response.text
    return response.text


def _text_from_html(html: str) -> str:
    if not html:
        return ""
    text = _TAG_PATTERN.sub(" ", html)
    return _SPACE_PATTERN.sub(" ", text).strip()


def _title_from_html(html: str, fallback: str | None = None) -> str:
    match = _TITLE_PATTERN.search(html or "")
    if match is None:
        return str(fallback or "").strip()
    title = _SPACE_PATTERN.sub(" ", _TAG_PATTERN.sub(" ", match.group(1))).strip()
    return title or str(fallback or "").strip()


def _job_title_from_url(url: str) -> str:
    match = _JOB_PATH_PATTERN.search(url)
    token = match.group(1) if match is not None else urlparse(url).path.rsplit("/", 1)[-1]
    cleaned = re.sub(r"[-_]+", " ", str(token or "").strip()).strip()
    return cleaned.title() if cleaned else "Job"


def _company_from_url(url: str) -> str:
    host = _normalize_domain(url) or ""
    if host.endswith(".myworkdayjobs.com"):
        host = host[: -len(".myworkdayjobs.com")]
    parts = [segment for segment in host.split(".") if segment and segment not in {"www", "jobs", "careers", "boards"}]
    candidate = parts[0] if parts else host
    cleaned = re.sub(r"[-_]+", " ", candidate).strip()
    return cleaned.title() if cleaned else "Unknown"


def _authorization_reject_reason(*texts: str) -> str | None:
    haystack = "\n".join(text for text in texts if text).strip()
    if not haystack:
        return None
    for reason, pattern in _AUTH_REJECT_PATTERNS:
        if pattern.search(haystack):
            return reason
    return None


def _compute_rehearsal_rank(
    title: str,
    description: str,
    *,
    preview_supported: bool,
    hard_reject_reason: str | None,
    auth_reject_reason: str | None,
    login_wall: bool,
) -> float:
    lowered_title = str(title or "").strip().lower()
    lowered_description = str(description or "").strip().lower()
    score = 100.0 if preview_supported else 20.0
    if any(token in lowered_title for token in _ENTRY_HINTS):
        score += 40.0
    if any(token in lowered_title for token in _SENIOR_HINTS):
        score -= 80.0
    if "intern" in lowered_title or "internship" in lowered_description:
        score -= 60.0
    if hard_reject_reason:
        score -= 100.0
    if auth_reject_reason:
        score -= 70.0
    if login_wall:
        score -= 40.0
    return max(0.0, score)


def _dead_apply_page(dom_snapshot: dict[str, Any], html: str) -> bool:
    lowered = _text_from_html(html).lower()
    if dom_snapshot.get("has_form") or dom_snapshot.get("has_submit_button") or dom_snapshot.get("has_confirmation"):
        return False
    return any(marker in lowered for marker in _DEAD_PAGE_HINTS) or "open jobs" in _title_from_html(html).lower()


def _empty_source_metrics() -> dict[str, SourceDiscoveryMetrics]:
    return {source_name: SourceDiscoveryMetrics() for source_name in LIVE_SOURCE_ORDER}


def _zero_result_reason(metrics: SourceDiscoveryMetrics) -> str | None:
    if metrics.boards_scanned <= 0:
        if metrics.errors > 0:
            return "scan_errors"
        return "not_scanned"
    if metrics.jobs_discovered <= 0:
        return "no_jobs_discovered"
    if metrics.eligible_jobs <= 0:
        return "no_jobs_retained"
    return None


def _source_warning(source_name: str, metrics: SourceDiscoveryMetrics) -> str | None:
    reason = metrics.zero_result_reason
    if reason == "not_scanned":
        return f"{source_name} is enabled but was not scanned (another source may have filled the quota first)."
    if reason == "scan_errors":
        return f"{source_name} had {metrics.errors} error(s) during scanning and found no boards."
    if reason == "no_jobs_discovered":
        return f"{source_name} scanned boards but discovered no jobs."
    if reason == "no_jobs_retained":
        return f"{source_name} discovered jobs but retained none after filtering."
    if metrics.errors:
        return f"{source_name} reported {metrics.errors} discovery error(s)."
    return None


async def _crawl_seed_targets(
    client: httpx.AsyncClient,
    workspace: FileWorkspace,
    *,
    max_pages: int = 20,
    crawl_depth: int = 2,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> SeedDiscovery:
    queue: deque[tuple[str, int, str]] = deque()
    visited: set[str] = set()
    seed = SeedDiscovery()
    seen_unsupported: set[str] = set()

    for url in _seed_urls(workspace):
        absolute = url if "://" in url else f"https://{url.strip('/')}"
        queue.append((absolute, 0, _normalize_domain(absolute) or ""))
    for domain in _seed_domains(workspace):
        root = f"https://{domain}"
        queue.append((root, 0, domain))
        sitemap_url = f"{root.rstrip('/')}/sitemap.xml"
        try:
            sitemap_xml = await _fetch_html(client, sitemap_url)
        except Exception:
            sitemap_xml = ""
        for sitemap_entry in parse_sitemap_urls(sitemap_xml):
            queue.append((sitemap_entry, 1, domain))

    while queue and seed.crawled_pages < max_pages:
        url, depth, seed_host = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            html = await _fetch_html(client, url)
        except Exception as exc:
            seed.errors.append(f"{url}: {exc}")
            continue
        seed.crawled_pages += 1
        source_host = _normalize_domain(url)
        corpus = f"{url}\n{html}"
        for source_name in LIVE_SOURCE_ORDER:
            tokens = _extract_board_tokens(source_name, corpus)
            if tokens and source_host and not _is_generic_source_host(source_host):
                seed.source_domains[source_name].add(source_host)
            seed.board_targets[source_name].update(tokens)
        links = parse_links(html, url)
        for link in links:
            classification = classify_job(
                source_kind="seed",
                apply_url=link,
                posting_url=link,
                source_adapter="seed_crawl",
                notes={},
            )
            family = classification.board_family.value
            if family in LIVE_SOURCE_ORDER:
                token = _extract_board_token(family, link)
                if token:
                    seed.board_targets[family].add(token)
                    if source_host and not _is_generic_source_host(source_host):
                        seed.source_domains[family].add(source_host)
            elif family in UNSUPPORTED_SKIP_FAMILIES and link not in seen_unsupported:
                seen_unsupported.add(link)
                seed.unsupported_urls.append({"url": link, "ats_family": family, "source_domain": source_host or seed_host})
        if depth < crawl_depth:
            crawlable = career_like_links(links, seed_host) if seed_host else []
            for link in crawlable:
                if link not in visited:
                    queue.append((link, depth + 1, seed_host))
        _emit_progress(
            progress_callback,
            {
                "phase": "seed_crawl",
                "crawled_pages": seed.crawled_pages,
                "board_targets": {source_name: sorted(values) for source_name, values in seed.board_targets.items()},
                "unsupported_total": len(seed.unsupported_urls),
                "errors_count": len(seed.errors),
            },
        )
    return seed


def _unsupported_job(url: str, *, ats_family: str, html: str, source_domain: str | None = None) -> InboxJob:
    title = _title_from_html(html, fallback=_job_title_from_url(url))
    text = _text_from_html(html)
    company = _company_from_url(source_domain or url)
    identifier = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    posting = build_normalized_job(
        company_name=company,
        title=title,
        source=ats_family,
        source_kind=ats_family,
        source_job_id=identifier,
        posting_url=url,
        apply_url=url,
        location_raw=None,
        employment_type=None,
        compensation=None,
        description=text,
        notes={"source_domain": source_domain or _normalize_domain(url), "seed_url": url},
    )
    job = _posting_to_inbox(posting)
    auth_reject = _authorization_reject_reason(text, title)
    return job.model_copy(
        update={
            "workflow_state": "screened_out",
            "ats_family": ats_family,
            "ats_preview_supported": False,
            "hard_reject_reason": f"unsupported_ats:{ats_family}",
            "auth_reject_reason": auth_reject,
            "rehearsal_eligible": False,
            "rehearsal_rank": 0.0,
            "discovery_method": "live_market:seed",
            "notes": {**dict(job.notes or {}), "seed_source_domain": source_domain},
        }
    )


async def _supported_job(posting, *, source_name: str, board: str, client: httpx.AsyncClient) -> tuple[InboxJob, str | None]:
    job = _posting_to_inbox(posting)
    apply_url = str(posting.apply_url or posting.posting_url)
    html = ""
    fetch_error: str | None = None
    try:
        html = await _fetch_html(client, apply_url)
    except Exception as exc:
        fetch_error = str(exc)
    dom_snapshot = analyze_dom_snapshot(html or "")
    text = _text_from_html(html or "")
    auth_reject = _authorization_reject_reason(posting.description, text)
    hard_reject_reason: str | None = None
    if _dead_apply_page(dom_snapshot, html):
        hard_reject_reason = "apply_page_unavailable"
    login_wall = bool(dom_snapshot.get("has_login_wall"))
    preview_supported = source_name in SUPPORTED_PREVIEW_FAMILIES and not login_wall and hard_reject_reason is None
    rehearsal_eligible = preview_supported and auth_reject is None
    workflow_state = "screened_out" if hard_reject_reason or auth_reject else "pending"
    return (
        job.model_copy(
            update={
                "workflow_state": workflow_state,
                "ats_family": source_name,
                "ats_preview_supported": preview_supported,
                "hard_reject_reason": hard_reject_reason,
                "auth_reject_reason": auth_reject,
                "login_wall_detected": login_wall,
                "rehearsal_eligible": rehearsal_eligible,
                "rehearsal_rank": _compute_rehearsal_rank(
                    posting.title,
                    posting.description,
                    preview_supported=preview_supported,
                    hard_reject_reason=hard_reject_reason,
                    auth_reject_reason=auth_reject,
                    login_wall=login_wall,
                ),
                "discovery_method": f"live_market:{source_name}",
                "notes": {**dict(job.notes or {}), "board": board, "apply_fetch_error": fetch_error},
            }
        ),
        fetch_error,
    )


def _round_robin_candidates(candidates: dict[str, list[InboxJob]], limit: int) -> list[InboxJob]:
    queues = {source_name: deque(items) for source_name, items in candidates.items()}
    selected: list[InboxJob] = []
    while len(selected) < limit and any(queues.get(source_name) for source_name in LIVE_SOURCE_ORDER):
        for source_name in LIVE_SOURCE_ORDER:
            if len(selected) >= limit:
                break
            queue = queues.get(source_name)
            if queue:
                selected.append(queue.popleft())
    return selected


def _source_metrics_snapshot(source_metrics: dict[str, SourceDiscoveryMetrics]) -> dict[str, dict[str, Any]]:
    return {
        source_name: metrics.model_dump(mode="json")
        for source_name, metrics in source_metrics.items()
    }


def _persist_discovery_state(
    workspace: FileWorkspace,
    *,
    seed: SeedDiscovery,
    source_metrics: dict[str, SourceDiscoveryMetrics],
    boards_with_jobs: dict[str, set[str]],
) -> BoardDiscoveryState:
    state = workspace.load_board_discovery_state()
    now = utcnow_iso()
    for source_name in LIVE_SOURCE_ORDER:
        record = state.sources[source_name]
        merged_boards = _ordered_unique(
            [
                *[_normalize_board_token(source_name, item) or "" for item in record.boards],
                *sorted(seed.board_targets.get(source_name, set())),
                *sorted(boards_with_jobs.get(source_name, set())),
            ]
        )
        merged_domains = _ordered_unique(
            [
                *[_normalize_domain(item) or "" for item in record.domains],
                *sorted(seed.source_domains.get(source_name, set())),
            ]
        )
        metrics = source_metrics[source_name].model_copy(update={"last_run_at": now})
        state.sources[source_name] = record.model_copy(
            update={
                "boards": [item for item in merged_boards if item],
                "domains": [item for item in merged_domains if item],
                "metrics": metrics,
            }
        )
    workspace.save_board_discovery_state(state)
    return state


async def discover_live_market(
    workspace: Path | FileWorkspace,
    *,
    limit: int = 50,
    candidate_limit: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    active_sources = _active_sources(ws)
    query = build_discovery_query(ws.load_profile())
    source_metrics = _empty_source_metrics()
    eligible_candidates: dict[str, list[InboxJob]] = {source_name: [] for source_name in LIVE_SOURCE_ORDER}
    skipped_candidates: list[InboxJob] = []
    boards_with_jobs: dict[str, set[str]] = {source_name: set() for source_name in LIVE_SOURCE_ORDER}
    discovered_boards: dict[str, set[str]] = {source_name: set() for source_name in LIVE_SOURCE_ORDER}
    duplicates = 0
    errors: list[str] = []
    unsupported_processed = 0
    raw_candidate_limit = max(1, int(candidate_limit or limit or 30))
    profile = ws.load_profile()
    seen_state = build_discovery_seen_state(ws)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        seed = await _crawl_seed_targets(client, ws, progress_callback=progress_callback)
        errors.extend(seed.errors)
        configured_scope = _filter_targets(_configured_targets(ws), active_sources)
        persisted_scope = _filter_targets(_persisted_targets(ws), active_sources)
        bootstrap_scope = _filter_targets(_builtin_targets(), active_sources)
        crawled_scope = _filter_targets(
            {source_name: sorted(values) for source_name, values in seed.board_targets.items()},
            active_sources,
        )
        merged_targets: dict[str, list[str]] = {}
        for source_name in active_sources:
            explicit = list(configured_scope.get(source_name) or [])
            if explicit:
                merged_targets[source_name] = explicit
                continue
            fallback_scope = fallback_targets(
                bootstrap={source_name: list(bootstrap_scope.get(source_name) or [])},
                configured={},
                persisted={source_name: list(persisted_scope.get(source_name) or [])},
                extra={source_name: list(crawled_scope.get(source_name) or [])},
            )
            if fallback_scope.get(source_name):
                merged_targets[source_name] = list(fallback_scope[source_name])
        total_boards = sum(len(values) for values in merged_targets.values())
        _emit_progress(
            progress_callback,
            {
                "phase": "seed_complete",
                "crawled_pages": seed.crawled_pages,
                "board_targets": {source_name: sorted(values) for source_name, values in seed.board_targets.items()},
                "boards_total": total_boards,
                "errors_count": len(errors),
                "unsupported_total": len(seed.unsupported_urls),
            },
        )

        source_queues = {source_name: deque(merged_targets.get(source_name, [])) for source_name in active_sources}
        while any(source_queues.get(source_name) for source_name in active_sources):
            for source_name in active_sources:
                board_queue = source_queues.get(source_name)
                if not board_queue:
                    continue
                board = board_queue.popleft()
                source_metrics[source_name].boards_scanned += 1
                discovered = 0
                accepted = 0
                rejected = 0
                try:
                    postings = await _adapter_for(source_name, [board]).discover(client, query)
                except Exception as exc:
                    source_metrics[source_name].errors += 1
                    errors.append(f"{source_name}/{board}: {exc}")
                    _emit_progress(
                        progress_callback,
                        {
                            "phase": "source_board",
                            "source": source_name,
                            "board": board,
                            "boards_completed": sum(metrics.boards_scanned for metrics in source_metrics.values()),
                            "boards_total": total_boards,
                            "errors_count": len(errors),
                            "error_counts": {name: metrics.errors for name, metrics in source_metrics.items()},
                            "source_metrics": _source_metrics_snapshot(source_metrics),
                        },
                    )
                    continue
                if postings:
                    discovered_boards[source_name].add(board)
                for posting, _payload in postings:
                    if discovered > 0 and sum(len(items) for items in eligible_candidates.values()) >= raw_candidate_limit:
                        break
                    discovered += 1
                    source_metrics[source_name].jobs_discovered += 1
                    job, fetch_error = await _supported_job(posting, source_name=source_name, board=board, client=client)
                    if fetch_error:
                        source_metrics[source_name].errors += 1
                        errors.append(f"{source_name}/{board}/{job.job_id}: {fetch_error}")
                    if _excluded_title(job.title, profile.targets.excluded_keywords):
                        rejected += 1
                        source_metrics[source_name].rejected_jobs += 1
                        continue
                    if _blocked_company(job.company, profile.targets.blocked_companies):
                        rejected += 1
                        source_metrics[source_name].rejected_jobs += 1
                        continue
                    if discovery_seen_match(job, seen_state):
                        duplicates += 1
                        continue
                    if job.rehearsal_eligible:
                        eligible_candidates[source_name].append(job)
                        accepted += 1
                        source_metrics[source_name].eligible_jobs += 1
                    else:
                        skipped_candidates.append(job)
                        rejected += 1
                        source_metrics[source_name].rejected_jobs += 1
                    remember_discovered_job(job, seen_state)
                    boards_with_jobs[source_name].add(board)
                    if discovered % 10 == 0:
                        _emit_progress(
                            progress_callback,
                            {
                                "phase": "source_board_progress",
                                "source": source_name,
                                "board": board,
                                "discovered": discovered,
                                "accepted_count": accepted,
                                "eligible_count": source_metrics[source_name].eligible_jobs,
                                "rejected_count": rejected,
                                "duplicates": duplicates,
                                "boards_completed": max(sum(metrics.boards_scanned for metrics in source_metrics.values()) - 1, 0),
                                "boards_total": total_boards,
                                "crawled_pages": seed.crawled_pages,
                                "source_counts": {name: len(eligible_candidates[name]) for name in LIVE_SOURCE_ORDER},
                                "eligible_by_source": {name: source_metrics[name].eligible_jobs for name in LIVE_SOURCE_ORDER},
                                "rejected_by_source": {name: source_metrics[name].rejected_jobs for name in LIVE_SOURCE_ORDER},
                                "error_counts": {name: source_metrics[name].errors for name in LIVE_SOURCE_ORDER},
                                "source_metrics": _source_metrics_snapshot(source_metrics),
                            },
                        )
                _emit_progress(
                    progress_callback,
                    {
                        "phase": "source_board",
                        "source": source_name,
                        "board": board,
                        "discovered": discovered,
                        "accepted_count": accepted,
                        "eligible_count": source_metrics[source_name].eligible_jobs,
                        "rejected_count": rejected,
                        "duplicates": duplicates,
                        "boards_completed": sum(metrics.boards_scanned for metrics in source_metrics.values()),
                        "boards_total": total_boards,
                        "crawled_pages": seed.crawled_pages,
                        "source_counts": {name: len(eligible_candidates[name]) for name in LIVE_SOURCE_ORDER},
                        "eligible_by_source": {name: source_metrics[name].eligible_jobs for name in LIVE_SOURCE_ORDER},
                        "rejected_by_source": {name: source_metrics[name].rejected_jobs for name in LIVE_SOURCE_ORDER},
                        "error_counts": {name: source_metrics[name].errors for name in LIVE_SOURCE_ORDER},
                        "source_metrics": _source_metrics_snapshot(source_metrics),
                    },
                )
            if sum(len(items) for items in eligible_candidates.values()) >= raw_candidate_limit:
                break

        for payload in seed.unsupported_urls:
            if len(skipped_candidates) + sum(len(items) for items in eligible_candidates.values()) >= raw_candidate_limit:
                break
            url = str(payload.get("url") or "").strip()
            ats_family = str(payload.get("ats_family") or "unknown").strip().lower() or "unknown"
            if not url:
                continue
            try:
                html = await _fetch_html(client, url)
            except Exception:
                html = ""
            skipped_candidates.append(_unsupported_job(url, ats_family=ats_family, html=html, source_domain=payload.get("source_domain")))
            unsupported_processed += 1
            _emit_progress(
                progress_callback,
                {
                    "phase": "unsupported_seed",
                    "unsupported_processed": unsupported_processed,
                    "unsupported_total": len(seed.unsupported_urls),
                },
            )

    for source_name in active_sources:
        source_metrics[source_name].boards_discovered = len(discovered_boards[source_name] | set(seed.board_targets.get(source_name, set())))
        zero_reason = _zero_result_reason(source_metrics[source_name])
        source_metrics[source_name].zero_result = zero_reason is not None
        source_metrics[source_name].zero_result_reason = zero_reason
        source_metrics[source_name].warning = _source_warning(source_name, source_metrics[source_name])

    persisted_state = _persist_discovery_state(ws, seed=seed, source_metrics=source_metrics, boards_with_jobs=boards_with_jobs)
    selected_jobs = _round_robin_candidates({source_name: eligible_candidates[source_name] for source_name in active_sources}, limit)
    if len(selected_jobs) < limit:
        selected_jobs.extend(skipped_candidates[: max(0, limit - len(selected_jobs))])

    created, updated = ws.upsert_inbox_jobs(selected_jobs)
    ws.append_scan_history(selected_jobs)
    saved_job_ids = [job.job_id for job in selected_jobs]
    eligible_job_ids = [job.job_id for job in selected_jobs if job.rehearsal_eligible]
    skipped_job_ids = [job.job_id for job in selected_jobs if not job.rehearsal_eligible]
    source_counts = {
        source_name: sum(1 for job in selected_jobs if job.source == source_name)
        for source_name in LIVE_SOURCE_ORDER
    }
    zero_result_sources = [source_name for source_name in active_sources if source_metrics[source_name].zero_result]
    warnings = [source_metrics[source_name].warning for source_name in active_sources if source_metrics[source_name].warning]

    return {
        "targets": merged_targets,
        "seed_summary": {
            "crawled_pages": seed.crawled_pages,
            "errors": list(errors),
            "unsupported_urls": len(seed.unsupported_urls),
            "board_targets": {source_name: sorted(values) for source_name, values in seed.board_targets.items()},
            "source_domains": {source_name: sorted(values) for source_name, values in seed.source_domains.items()},
        },
        "discovered": sum(source_metrics[source_name].jobs_discovered for source_name in LIVE_SOURCE_ORDER) + unsupported_processed,
        "new_jobs": created,
        "updated_jobs": updated,
        "duplicates": duplicates,
        "saved_job_ids": saved_job_ids,
        "eligible_job_ids": eligible_job_ids,
        "skipped_job_ids": skipped_job_ids,
        "rejected_count": sum(source_metrics[source_name].rejected_jobs for source_name in LIVE_SOURCE_ORDER),
        "errors": list(errors),
        "warnings": warnings,
        "source_counts": source_counts,
        "eligible_by_source": {source_name: source_metrics[source_name].eligible_jobs for source_name in LIVE_SOURCE_ORDER},
        "rejected_by_source": {source_name: source_metrics[source_name].rejected_jobs for source_name in LIVE_SOURCE_ORDER},
        "error_counts": {source_name: source_metrics[source_name].errors for source_name in LIVE_SOURCE_ORDER},
        "zero_result_sources": zero_result_sources,
        "source_metrics": {
            source_name: source_metrics[source_name].model_dump(mode="json")
            for source_name in LIVE_SOURCE_ORDER
        },
        "persisted_discovery": persisted_state.model_dump(mode="json"),
        "selected_jobs": [job.model_dump(mode="json") for job in selected_jobs],
    }
