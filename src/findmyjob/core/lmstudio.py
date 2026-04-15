from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlparse

import httpx

LMSTUDIO_PROVIDER = "lmstudio"
LMSTUDIO_DEFAULT_HOST = "http://127.0.0.1:1234"
LMSTUDIO_DEFAULT_BASE_URL = f"{LMSTUDIO_DEFAULT_HOST}/v1"
LMSTUDIO_AUTO_MODEL = "auto"
LMSTUDIO_DEFAULT_WRITER_MODEL = "qwen3.5-35b-a3b-uncensored-hauhaucs-aggressive"
LMSTUDIO_DEFAULT_SCREENING_MODEL = "gemma-4-e2b-it"


@dataclass(frozen=True, slots=True)
class ResolvedLocalBaseURL:
    requested_url: str
    canonical_base_url: str
    candidates: tuple[str, ...]
    models_payload: dict[str, Any]


def lmstudio_models_payload_is_catalog(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("data"), list)


def lmstudio_model_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not lmstudio_models_payload_is_catalog(payload):
        return []
    return [item for item in payload.get("data", []) if isinstance(item, dict)]


def lmstudio_available_model_ids(payload: dict[str, Any]) -> list[str]:
    return [
        str(item.get("id") or "").strip()
        for item in lmstudio_model_entries(payload)
        if str(item.get("id") or "").strip()
    ]


def lmstudio_active_model_id(payload: dict[str, Any]) -> str | None:
    model_ids = lmstudio_available_model_ids(payload)
    return model_ids[0] if model_ids else None


def lmstudio_model_selection_is_auto(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"", LMSTUDIO_AUTO_MODEL, "active", "current"}


def normalize_lmstudio_model_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    if ":" in normalized:
        normalized = normalized.split(":", 1)[0]
    if normalized.endswith(".gguf"):
        normalized = normalized[:-5]
    if normalized.endswith("-gguf"):
        normalized = normalized[:-5]
    normalized = normalized.replace("_", "-")
    normalized = re.sub(r"[^a-z0-9-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-")


def lmstudio_model_name_matches(requested: str | None, candidate: str | None) -> bool:
    requested_name = normalize_lmstudio_model_name(requested)
    candidate_name = normalize_lmstudio_model_name(candidate)
    if not requested_name or not candidate_name:
        return False
    if requested_name == candidate_name:
        return True
    return candidate_name.startswith(requested_name + "-")


def resolve_lmstudio_model_id(requested: str | None, payload: dict[str, Any]) -> str | None:
    exact_requested = str(requested or "").strip()
    if not exact_requested:
        return None
    for item in lmstudio_model_entries(payload):
        model_id = str(item.get("id") or "").strip()
        aliases = [str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()]
        names = [model_id, *aliases]
        if any(exact_requested == candidate for candidate in names):
            return model_id
    for item in lmstudio_model_entries(payload):
        model_id = str(item.get("id") or "").strip()
        aliases = [str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()]
        names = [model_id, *aliases]
        if any(lmstudio_model_name_matches(exact_requested, candidate) for candidate in names):
            return model_id
    return None


def resolve_lmstudio_model_id_or_default(requested: str | None, payload: dict[str, Any]) -> str | None:
    if lmstudio_model_selection_is_auto(requested):
        return lmstudio_active_model_id(payload)
    return resolve_lmstudio_model_id(requested, payload) or lmstudio_active_model_id(payload)


def is_lmstudio_provider(provider: str | None) -> bool:
    return str(provider or "").strip().lower() == LMSTUDIO_PROVIDER


def normalize_http_root(value: str | None, *, default_url: str = LMSTUDIO_DEFAULT_HOST) -> str:
    candidate = str(value or "").strip() or default_url
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid HTTP base URL: {value!r}")
    path = parsed.path.rstrip("/")
    normalized = f"{parsed.scheme}://{parsed.netloc}"
    if path and path != "/":
        normalized = f"{normalized}{path}"
    return normalized


def canonical_lmstudio_base_url(value: str | None) -> str:
    root = normalize_http_root(value, default_url=LMSTUDIO_DEFAULT_HOST)
    if root.endswith("/v1"):
        return root
    return f"{root}/v1"


def lmstudio_probe_candidates(value: str | None) -> tuple[str, ...]:
    root = normalize_http_root(value, default_url=LMSTUDIO_DEFAULT_HOST)
    if root.endswith("/v1"):
        raw = root[:-3]
        canonical = root
    else:
        raw = root
        canonical = f"{root}/v1"
    if raw == canonical:
        return (raw,)
    return (raw, canonical)


def probe_lmstudio_base_url(
    value: str | None,
    *,
    timeout: float = 15.0,
    api_key: str | None = None,
) -> ResolvedLocalBaseURL:
    requested_url = str(value or "").strip() or LMSTUDIO_DEFAULT_HOST
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: Exception | None = None
    candidates = lmstudio_probe_candidates(requested_url)
    for candidate in candidates:
        try:
            with httpx.Client(timeout=timeout, headers=headers) as client:
                response = client.get(f"{candidate.rstrip('/')}/models")
                response.raise_for_status()
                payload = response.json()
            if not lmstudio_models_payload_is_catalog(payload):
                raise ValueError(
                    f"Unexpected model catalog payload from {candidate.rstrip('/')}/models"
                )
            return ResolvedLocalBaseURL(
                requested_url=requested_url,
                canonical_base_url=candidate,
                candidates=candidates,
                models_payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - surface the last probe failure
            last_error = exc

    tried = ", ".join(candidates)
    detail = f"{type(last_error).__name__}: {last_error}" if last_error is not None else "unknown error"
    raise ValueError(
        f"Unable to reach LM Studio at {requested_url}. Tried {tried}. Last error: {detail}"
    ) from last_error
