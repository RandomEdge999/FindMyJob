from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from findmyjob.filefirst.portal_defaults import bootstrap_board_targets
from findmyjob.filefirst.workspace import FileWorkspace

SOURCE_ORDER = ("greenhouse", "lever", "ashby")
SUPPORTED_SOURCES = set(SOURCE_ORDER)

_TOKEN_VALUE = r"([A-Za-z0-9][A-Za-z0-9._-]*)"
_TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(rf"(?:boards|job-boards)\.greenhouse\.io/{_TOKEN_VALUE}", re.IGNORECASE),
    "lever": re.compile(rf"(?:jobs|api)\.lever\.co/(?:v0/postings/)?{_TOKEN_VALUE}", re.IGNORECASE),
    "ashby": re.compile(rf"jobs\.ashbyhq\.com/{_TOKEN_VALUE}", re.IGNORECASE),
}
_DIRECT_TOKEN_PATTERN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)")

_GENERIC_SOURCE_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "api.lever.co",
    "jobs.ashbyhq.com",
}


@dataclass(slots=True)
class SourceTargetScope:
    configured: list[str]
    persisted: list[str]
    bootstrap: list[str]

    @property
    def combined(self) -> list[str]:
        return ordered_unique([*self.configured, *self.persisted, *self.bootstrap])


def ordered_unique(values: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def normalize_domain(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = str(parsed.netloc or parsed.path).strip().lower()
    if not host:
        return None
    return host.split("/", 1)[0].strip()


def is_generic_source_host(domain: str | None) -> bool:
    host = str(domain or "").strip().lower()
    return bool(host and host in _GENERIC_SOURCE_HOSTS)


def extract_board_tokens(source: str, value: str | None) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    pattern = _TOKEN_PATTERNS.get(source)
    if pattern is None:
        return set()
    return {
        match.group(1).strip().strip("/").lower()
        for match in pattern.finditer(text)
        if match.group(1).strip().strip("/")
    }


def extract_board_token(source: str, value: str | None) -> str | None:
    tokens = list(extract_board_tokens(source, value))
    return tokens[0] if tokens else None


def normalize_board_token(source: str, value: str | None) -> str | None:
    token = extract_board_token(source, value)
    if token:
        return token
    cleaned = str(value or "").strip().strip("/").strip("'\"")
    if not cleaned:
        return None
    match = _DIRECT_TOKEN_PATTERN.search(cleaned)
    if match is None:
        return None
    return match.group(1).strip().lower() or None


def canonical_board_url(source: str, board: str) -> str | None:
    token = normalize_board_token(source, board)
    if not token:
        return None
    if source == "greenhouse":
        return f"https://boards.greenhouse.io/{token}"
    if source == "lever":
        return f"https://jobs.lever.co/{token}"
    if source == "ashby":
        return f"https://jobs.ashbyhq.com/{token}"
    return None


def builtin_targets() -> dict[str, list[str]]:
    return bootstrap_board_targets()


def _direct_seed_targets(values: Iterable[str]) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for source_name in SOURCE_ORDER:
        tokens: list[str] = []
        for value in values:
            token = extract_board_token(source_name, value)
            if token and token not in tokens:
                tokens.append(token)
        if tokens:
            targets[source_name] = tokens
    return targets


def seed_urls(workspace: FileWorkspace) -> list[str]:
    portals = workspace.load_portals()
    urls: list[str] = []
    for source_name in SOURCE_ORDER:
        config = portals.sources.get(source_name)
        if config is None:
            continue
        for value in getattr(config, "seed_urls", []) or []:
            cleaned = str(value or "").strip()
            if cleaned:
                urls.append(cleaned)
        for value in getattr(config, "boards", []) or []:
            canonical = canonical_board_url(source_name, str(value or ""))
            if canonical:
                urls.append(canonical)
    for company in portals.tracked_companies:
        if not company.enabled:
            continue
        for value in (company.careers_url, company.api):
            cleaned = str(value or "").strip()
            if cleaned:
                urls.append(cleaned)
    return ordered_unique(urls)


def seed_domains(workspace: FileWorkspace) -> list[str]:
    portals = workspace.load_portals()
    persisted = workspace.load_board_discovery_state()
    domains: list[str] = []
    for source_name in SOURCE_ORDER:
        config = portals.sources.get(source_name)
        if config is None:
            continue
        for value in getattr(config, "seed_domains", []) or []:
            normalized = normalize_domain(value)
            if normalized:
                domains.append(normalized)
        record = persisted.sources.get(source_name)
        for value in list(record.domains if record is not None else []):
            normalized = normalize_domain(value)
            if normalized:
                domains.append(normalized)
    for company in portals.tracked_companies:
        if not company.enabled:
            continue
        for value in (company.careers_url, company.api):
            normalized = normalize_domain(value)
            if normalized:
                domains.append(normalized)
    return ordered_unique(domains)


def configured_targets(workspace: FileWorkspace) -> dict[str, list[str]]:
    portals = workspace.load_portals()
    configured: dict[str, list[str]] = {}
    for source_name in SOURCE_ORDER:
        config = portals.sources.get(source_name)
        if config is None:
            continue
        values = [normalize_board_token(source_name, item) for item in getattr(config, "boards", []) or []]
        tokens = [value for value in values if value]
        if tokens:
            configured[source_name] = ordered_unique(tokens)
        extracted = _direct_seed_targets([str(item or "") for item in getattr(config, "seed_urls", []) or []])
        if extracted.get(source_name):
            configured[source_name] = ordered_unique([*(configured.get(source_name) or []), *extracted[source_name]])
    for company in portals.tracked_companies:
        if not company.enabled:
            continue
        company_urls = [
            str(value or "").strip()
            for value in (company.careers_url, company.api)
            if str(value or "").strip()
        ]
        source_name = str(company.source or "").strip().lower()
        if source_name in SOURCE_ORDER:
            token = normalize_board_token(source_name, company.board or "")
            if token:
                configured[source_name] = ordered_unique([*(configured.get(source_name) or []), token])
            extracted = _direct_seed_targets(company_urls)
            if extracted.get(source_name):
                configured[source_name] = ordered_unique([*(configured.get(source_name) or []), *extracted[source_name]])
            continue
        configured = merge_targets(configured, _direct_seed_targets(company_urls))
    return {source_name: list(values) for source_name, values in configured.items() if values}


def persisted_targets(workspace: FileWorkspace) -> dict[str, list[str]]:
    state = workspace.load_board_discovery_state()
    persisted: dict[str, list[str]] = {}
    for source_name in SOURCE_ORDER:
        record = state.sources.get(source_name)
        if record is None:
            continue
        values = [normalize_board_token(source_name, item) for item in record.boards]
        tokens = [value for value in values if value]
        if tokens:
            persisted[source_name] = ordered_unique(tokens)
    return persisted


def requested_sources(workspace: FileWorkspace) -> list[str]:
    return ordered_unique(
        str(item or "").strip().lower()
        for item in getattr(workspace.load_profile().runtime.automation, "production_sources", []) or []
        if str(item or "").strip().lower() in SOURCE_ORDER
    )


def enabled_sources(workspace: FileWorkspace) -> list[str]:
    portals = workspace.load_portals()
    return [
        source_name
        for source_name in SOURCE_ORDER
        if bool(getattr(portals.sources.get(source_name), "enabled", False))
    ]


def active_sources(workspace: FileWorkspace) -> list[str]:
    requested = requested_sources(workspace)
    enabled = enabled_sources(workspace)
    if requested and enabled:
        overlap = [source_name for source_name in SOURCE_ORDER if source_name in requested and source_name in enabled]
        if overlap:
            return overlap
        return enabled
    if requested:
        return [source_name for source_name in SOURCE_ORDER if source_name in requested]
    if enabled:
        return enabled
    return list(SOURCE_ORDER)


def merge_targets(*parts: dict[str, Iterable[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {name: [] for name in SOURCE_ORDER}
    for part in parts:
        for source_name, values in part.items():
            if source_name not in merged:
                continue
            for value in values:
                token = normalize_board_token(source_name, str(value or ""))
                if token and token not in merged[source_name]:
                    merged[source_name].append(token)
    return {name: values for name, values in merged.items() if values}


def fallback_targets(
    *,
    bootstrap: dict[str, Iterable[str]] | None = None,
    configured: dict[str, Iterable[str]] | None = None,
    persisted: dict[str, Iterable[str]] | None = None,
    extra: dict[str, Iterable[str]] | None = None,
) -> dict[str, list[str]]:
    bootstrap = bootstrap or {}
    configured = configured or {}
    persisted = persisted or {}
    extra = extra or {}
    merged: dict[str, list[str]] = {}
    for source_name in SOURCE_ORDER:
        combined = ordered_unique(
            [
                *(configured.get(source_name) or []),
                *(persisted.get(source_name) or []),
                *(bootstrap.get(source_name) or []),
                *(extra.get(source_name) or []),
            ]
        )
        if combined:
            merged[source_name] = [
                token
                for token in (
                    normalize_board_token(source_name, value)
                    for value in combined
                )
                if token
            ]
    return {source_name: values for source_name, values in merged.items() if values}


def filter_targets(targets: dict[str, list[str]], active: Iterable[str]) -> dict[str, list[str]]:
    active_set = set(active)
    return {
        source_name: list(values)
        for source_name, values in targets.items()
        if source_name in active_set
    }


def resolve_targets(
    workspace: Path | FileWorkspace,
    *,
    source: str | None = None,
    board: str | None = None,
    extra_targets: dict[str, Iterable[str]] | None = None,
    include_bootstrap: bool = True,
    include_persisted: bool = True,
    active_only: bool = True,
) -> dict[str, list[str]]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    source_name = str(source or "").strip().lower() or None
    if source_name and board:
        token = normalize_board_token(source_name, board)
        if token and source_name in SUPPORTED_SOURCES:
            return {source_name: [token]}
        return {}
    merged = fallback_targets(
        bootstrap=builtin_targets() if include_bootstrap else {},
        configured=configured_targets(ws),
        persisted=persisted_targets(ws) if include_persisted else {},
        extra=extra_targets or {},
    )
    if active_only:
        merged = filter_targets(merged, active_sources(ws))
    if source_name:
        return {source_name: list(merged.get(source_name) or [])} if merged.get(source_name) else {}
    return merged


def scope_targets(workspace: Path | FileWorkspace, source_name: str) -> SourceTargetScope:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    source_key = str(source_name or "").strip().lower()
    return SourceTargetScope(
        configured=list(configured_targets(ws).get(source_key) or []),
        persisted=list(persisted_targets(ws).get(source_key) or []),
        bootstrap=list(builtin_targets().get(source_key) or []),
    )
