from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from tomlkit import document, dumps, item, parse, table

from findmyjob.core.config import AppConfig
from findmyjob.core.enums import ModelRole
from findmyjob.core.lmstudio import (
    LMSTUDIO_AUTO_MODEL,
    LMSTUDIO_DEFAULT_HOST,
    LMSTUDIO_PROVIDER,
    lmstudio_available_model_ids,
    probe_lmstudio_base_url,
    resolve_lmstudio_model_id_or_default,
)
from findmyjob.core.types import ModelProfile
from findmyjob.filefirst.models import LocalModelSettings
from findmyjob.filefirst.workspace import FileWorkspace
from findmyjob.model_router.router import ModelRouter

DEFAULT_REMOTE_MAX_TOKENS = 8192
LMSTUDIO_SCREEN_PREFIX = "lmstudio-screen"
LMSTUDIO_DRAFT_PREFIX = "lmstudio-draft"
_FAST_LMSTUDIO_PROBE_TIMEOUT_SECONDS = 0.75
_MODEL_FAMILY_ROLES: dict[str, tuple[ModelRole, ...]] = {
    "screening": (
        ModelRole.TEXT_ROUTER,
        ModelRole.CLASSIFIER,
        ModelRole.EXTRACTOR,
    ),
    "drafting": (
        ModelRole.WRITER,
        ModelRole.RESUME_WRITER,
        ModelRole.COVER_LETTER_WRITER,
    ),
    "qa": (ModelRole.QUESTION_ANSWERER,),
}


def _lmstudio_profile(
    *,
    name: str,
    role: ModelRole,
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    supports_structured_output: bool,
    policy_tags: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "role": role.value,
        "provider": LMSTUDIO_PROVIDER,
        "model": model,
        "local": True,
        "transport": "local_http",
        "base_url": base_url,
        "api_key_env": None,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "supports_structured_output": supports_structured_output,
        "fallback_chain": [],
        "policy_tags": policy_tags,
    }


def _is_legacy_local_binding(profile: ModelProfile) -> bool:
    return profile.name.startswith("local-gemma-")


def _is_launch_contract_profile(profile: ModelProfile) -> bool:
    return not _is_legacy_local_binding(profile)



def load_app_config(workspace: FileWorkspace | Path) -> AppConfig:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    return AppConfig.load(ws.root)


def _local_model_settings(workspace: FileWorkspace | Path) -> LocalModelSettings:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    try:
        return ws.load_profile().runtime.model
    except Exception:
        return LocalModelSettings(
            base_url=LMSTUDIO_DEFAULT_HOST,
            model=LMSTUDIO_AUTO_MODEL,
            max_tokens=DEFAULT_REMOTE_MAX_TOKENS,
            preferred_context_window=131072,
        )


@lru_cache(maxsize=16)
def _split_model_defaults_cached(
    base_url: str,
    model: str,
    max_tokens: int,
) -> dict[str, dict[str, Any]]:
    resolved_base_url = str(base_url or "").strip() or LMSTUDIO_DEFAULT_HOST
    writer_model = str(model or "").strip() or LMSTUDIO_AUTO_MODEL
    screening_model = LMSTUDIO_AUTO_MODEL
    try:
        discovered = probe_lmstudio_base_url(resolved_base_url, timeout=_FAST_LMSTUDIO_PROBE_TIMEOUT_SECONDS)
        resolved_base_url = discovered.canonical_base_url
        writer_model = resolve_lmstudio_model_id_or_default(writer_model, discovered.models_payload) or writer_model
        screening_model = (
            resolve_lmstudio_model_id_or_default(screening_model, discovered.models_payload) or screening_model
        )
        if screening_model == writer_model:
            available = lmstudio_available_model_ids(discovered.models_payload)
            alternatives = [m for m in available if m != writer_model]
            if alternatives:
                screening_model = alternatives[0]
    except Exception:
        pass
    effective_max_tokens = max(DEFAULT_REMOTE_MAX_TOKENS, int(max_tokens or 0))
    return {
        f"{LMSTUDIO_SCREEN_PREFIX}-text-router": _lmstudio_profile(
            name=f"{LMSTUDIO_SCREEN_PREFIX}-text-router",
            role=ModelRole.TEXT_ROUTER,
            model=screening_model,
            base_url=resolved_base_url,
            temperature=0.0,
            max_tokens=effective_max_tokens,
            supports_structured_output=True,
            policy_tags=["screen", "routing"],
        ),
        f"{LMSTUDIO_SCREEN_PREFIX}-classifier": _lmstudio_profile(
            name=f"{LMSTUDIO_SCREEN_PREFIX}-classifier",
            role=ModelRole.CLASSIFIER,
            model=screening_model,
            base_url=resolved_base_url,
            temperature=0.0,
            max_tokens=effective_max_tokens,
            supports_structured_output=True,
            policy_tags=["screen", "classifier"],
        ),
        f"{LMSTUDIO_SCREEN_PREFIX}-extractor": _lmstudio_profile(
            name=f"{LMSTUDIO_SCREEN_PREFIX}-extractor",
            role=ModelRole.EXTRACTOR,
            model=screening_model,
            base_url=resolved_base_url,
            temperature=0.0,
            max_tokens=effective_max_tokens,
            supports_structured_output=True,
            policy_tags=["screen", "extractor"],
        ),
        f"{LMSTUDIO_DRAFT_PREFIX}-writer": _lmstudio_profile(
            name=f"{LMSTUDIO_DRAFT_PREFIX}-writer",
            role=ModelRole.WRITER,
            model=writer_model,
            base_url=resolved_base_url,
            temperature=0.7,
            max_tokens=effective_max_tokens,
            supports_structured_output=True,
            policy_tags=["draft", "writer"],
        ),
        f"{LMSTUDIO_DRAFT_PREFIX}-resume-writer": _lmstudio_profile(
            name=f"{LMSTUDIO_DRAFT_PREFIX}-resume-writer",
            role=ModelRole.RESUME_WRITER,
            model=writer_model,
            base_url=resolved_base_url,
            temperature=0.7,
            max_tokens=effective_max_tokens,
            supports_structured_output=True,
            policy_tags=["draft", "resume"],
        ),
        f"{LMSTUDIO_DRAFT_PREFIX}-cover-letter-writer": _lmstudio_profile(
            name=f"{LMSTUDIO_DRAFT_PREFIX}-cover-letter-writer",
            role=ModelRole.COVER_LETTER_WRITER,
            model=writer_model,
            base_url=resolved_base_url,
            temperature=0.7,
            max_tokens=effective_max_tokens,
            supports_structured_output=True,
            policy_tags=["draft", "cover_letter"],
        ),
        f"{LMSTUDIO_DRAFT_PREFIX}-question-answerer": _lmstudio_profile(
            name=f"{LMSTUDIO_DRAFT_PREFIX}-question-answerer",
            role=ModelRole.QUESTION_ANSWERER,
            model=screening_model,
            base_url=resolved_base_url,
            temperature=0.7,
            max_tokens=effective_max_tokens,
            supports_structured_output=False,
            policy_tags=["draft", "question_answering"],
        ),
    }


def _default_profiles_for_router(workspace: FileWorkspace | Path) -> dict[str, ModelProfile]:
    settings = _local_model_settings(workspace)
    return {
        name: ModelProfile.model_validate(payload)
        for name, payload in split_model_defaults(settings).items()
    }


def _effective_model_config(workspace: FileWorkspace | Path) -> AppConfig:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    try:
        config = load_app_config(ws)
    except Exception:
        config = AppConfig()
    default_profiles = _default_profiles_for_router(ws)
    merged_models: dict[str, ModelProfile] = {}
    # All non-legacy workspace profiles participate in launch inspection.
    # The router decides whether a given binding is pass / warning / fail.
    for name, profile in config.models.items():
        if _is_legacy_local_binding(profile):
            continue
        if not _is_launch_contract_profile(profile):
            continue
        merged_models[name] = profile
    # Fill in local defaults only for roles not already covered by config.
    covered_roles = {profile.role for profile in merged_models.values()}
    for name, profile in default_profiles.items():
        if profile.role in covered_roles:
            continue
        merged_models[name] = profile
    return config.model_copy(deep=True, update={"models": merged_models})


def load_model_router(workspace: FileWorkspace | Path) -> ModelRouter | None:
    return ModelRouter(_effective_model_config(workspace))


def has_role_binding(workspace: FileWorkspace | Path, role: ModelRole) -> bool:
    router = load_model_router(workspace)
    if router is None:
        return False
    try:
        router.get_profile(role=role)
    except ValueError:
        return False
    return True


def mode_system_prompt(workspace: FileWorkspace | Path, mode_name: str) -> str:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    shared = (ws.modes_dir / "_shared.md").read_text(encoding="utf-8")
    mode = (ws.modes_dir / f"{mode_name}.md").read_text(encoding="utf-8")
    return f"{shared.strip()}\n\n{mode.strip()}\n\nOperate inside a local file-first career workspace."


async def run_mode_json(
    workspace: FileWorkspace | Path,
    *,
    role: ModelRole,
    mode_name: str,
    context: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    router = load_model_router(workspace)
    if router is None:
        raise RuntimeError("No advanced model router is configured.")
    payload, profile_name = await router.generate_json_with_profile(
        role,
        "Context JSON:\n" + json.dumps(context, indent=2, default=str),
        system_prompt=mode_system_prompt(workspace, mode_name),
    )
    return payload, profile_name


async def run_mode_json_for_roles(
    workspace: FileWorkspace | Path,
    *,
    preferred_roles: list[ModelRole],
    mode_name: str,
    context: dict[str, Any],
) -> tuple[dict[str, Any], str, ModelRole]:
    router = load_model_router(workspace)
    if router is None:
        raise RuntimeError("No advanced model router is configured.")
    resolved_role: ModelRole | None = None
    for candidate in preferred_roles:
        try:
            router.get_profile(role=candidate)
        except ValueError:
            continue
        resolved_role = candidate
        break
    if resolved_role is None:
        joined = ", ".join(role.value for role in preferred_roles)
        raise RuntimeError(f"No advanced model router binding is configured for any of: {joined}")
    payload, profile_name = await router.generate_json_with_profile(
        resolved_role,
        "Context JSON:\n" + json.dumps(context, indent=2, default=str),
        system_prompt=mode_system_prompt(workspace, mode_name),
    )
    return payload, profile_name, resolved_role


def advanced_models_payload(workspace: FileWorkspace | Path) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    config_path = ws.workspace_config_path
    payload: dict[str, Any] = {
        "config_path": ws.relative_path(config_path),
        "exists": config_path.exists(),
        "profiles": [],
        "launch_profile": None,
        "recommended_split_defaults": split_model_defaults(_local_model_settings(ws)),
    }
    try:
        config = _effective_model_config(ws)
        router = ModelRouter(config)
    except Exception as exc:
        payload["error"] = str(exc)
        return payload
    inspection = router.inspect_profiles()
    launch_profile = router.inspect_launch_profile()
    role_bindings: dict[str, str] = {}
    for role_status in launch_profile.roles:
        if role_status.profile_name:
            role_bindings[role_status.role] = role_status.profile_name
    for profile in config.models.values():
        role_bindings.setdefault(profile.role.value, profile.name)
    payload.update(
        {
            "profiles": inspection.get("profiles", []),
            "missing_required_roles": inspection.get("missing_required_roles", []),
            "duplicate_roles": inspection.get("duplicate_roles", {}),
            "role_bindings": role_bindings,
            "launch_profile": launch_profile.model_dump(mode="json"),
        }
    )
    return payload


def split_model_defaults(settings: LocalModelSettings | None = None) -> dict[str, dict[str, Any]]:
    resolved = settings or LocalModelSettings()
    base_url = str(getattr(resolved, "base_url", None) or "").strip() or LMSTUDIO_DEFAULT_HOST
    writer_model = str(getattr(resolved, "model", None) or "").strip() or LMSTUDIO_AUTO_MODEL
    max_tokens = max(DEFAULT_REMOTE_MAX_TOKENS, int(getattr(resolved, "max_tokens", 0) or 0))
    return copy.deepcopy(_split_model_defaults_cached(base_url, writer_model, max_tokens))


def install_recommended_split_profiles(workspace: FileWorkspace | Path) -> dict[str, Any]:
    installed: list[dict[str, Any]] = []
    for name, payload in split_model_defaults(_local_model_settings(workspace)).items():
        profile = save_workspace_model_profile(workspace, name, payload)
        installed.append(profile.model_dump(mode="json"))
    return {
        "saved": True,
        "installed": installed,
    }


def save_workspace_model_family(
    workspace: FileWorkspace | Path,
    family_id: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
    transport: str | None = None,
    api_key_env: str | None = None,
) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()

    normalized_family = str(family_id or "").strip().lower()
    roles = _MODEL_FAMILY_ROLES.get(normalized_family)
    if not roles:
        raise ValueError(f"Unknown model family: {family_id}")

    defaults = split_model_defaults(_local_model_settings(ws))
    saved_profiles: list[dict[str, Any]] = []
    resolved_model = str(model or "").strip()
    resolved_base_url = str(base_url or "").strip()
    resolved_provider = str(provider or "").strip()
    resolved_transport = str(transport or "").strip()
    resolved_api_key_env = str(api_key_env or "").strip()

    for role in roles:
        default_entry = next(
            (
                (name, payload)
                for name, payload in defaults.items()
                if str(payload.get("role") or "").strip() == role.value
            ),
            None,
        )
        if default_entry is None:
            raise ValueError(f"No default LM Studio profile template exists for role: {role.value}")
        name, payload = default_entry
        updated_payload = copy.deepcopy(payload)
        if resolved_model:
            updated_payload["model"] = resolved_model
        if resolved_base_url:
            updated_payload["base_url"] = resolved_base_url
        if resolved_provider:
            updated_payload["provider"] = resolved_provider
        if resolved_transport:
            updated_payload["transport"] = resolved_transport
        if resolved_api_key_env:
            updated_payload["api_key_env"] = resolved_api_key_env
        elif resolved_transport == "local_http":
            updated_payload["api_key_env"] = None
        profile = save_workspace_model_profile(ws, name, updated_payload)
        saved_profiles.append(profile.model_dump(mode="json"))

    return {
        "saved": True,
        "family": normalized_family,
        "profiles": saved_profiles,
    }


def save_workspace_model_profile(workspace: FileWorkspace | Path, name: str, payload: dict[str, Any]) -> ModelProfile:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    config_path = ws.workspace_config_path
    if config_path.exists():
        doc = parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = document()
        doc["workspace"] = "."
    if "models" not in doc:
        doc["models"] = table()
    models_table = doc["models"]
    current_role = None
    try:
        existing = load_app_config(ws).models.get(name)
    except Exception:
        existing = None
    if existing is not None:
        current_role = existing.role.value
    role = str(payload.get("role") or current_role or "").strip()
    if not role:
        raise ValueError("Model profile role is required for workspace config profiles.")
    profile_payload: dict[str, Any] = {
        "name": name,
        "role": role,
        "provider": str(payload.get("provider") or LMSTUDIO_PROVIDER).strip() or LMSTUDIO_PROVIDER,
        "model": str(payload.get("model") or "").strip(),
        "local": bool(payload.get("local", False)),
        "transport": str(payload.get("transport") or "").strip() or None,
        "temperature": float(payload.get("temperature", 0.0) or 0.0),
        "max_tokens": int(payload["max_tokens"]) if payload.get("max_tokens") not in (None, "") else None,
        "supports_structured_output": bool(payload.get("supports_structured_output", False)),
        "fallback_chain": [str(item).strip() for item in list(payload.get("fallback_chain") or []) if str(item).strip()],
        "policy_tags": [str(item).strip() for item in list(payload.get("policy_tags") or []) if str(item).strip()],
        "base_url": str(payload.get("base_url") or "").strip() or None,
        "api_key_env": str(payload.get("api_key_env") or "").strip() or None,
        "command": [str(item) for item in list(payload.get("command") or []) if str(item).strip()],
        "working_dir": str(payload.get("working_dir") or "").strip() or None,
    }
    if profile_payload["provider"].lower() == LMSTUDIO_PROVIDER or profile_payload["transport"] == "local_http":
        resolved = probe_lmstudio_base_url(profile_payload["base_url"])
        profile_payload["provider"] = LMSTUDIO_PROVIDER
        profile_payload["transport"] = "local_http"
        profile_payload["local"] = True
        profile_payload["base_url"] = resolved.canonical_base_url
        profile_payload["model"] = str(profile_payload["model"] or "").strip() or LMSTUDIO_AUTO_MODEL
        profile_payload["api_key_env"] = None
    if not profile_payload["model"]:
        raise ValueError("Model profile model is required.")
    profile_table = table()
    for key, value in profile_payload.items():
        if value is None:
            continue
        if isinstance(value, list):
            profile_table[key] = item(value)
        else:
            profile_table[key] = value
    models_table[name] = profile_table
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(dumps(doc), encoding="utf-8")
    try:
        config = load_app_config(ws)
    except ValidationError as exc:
        raise ValueError(f"Workspace config is invalid and blocked model save: {exc}") from exc
    profile = config.models.get(name)
    if profile is None:
        raise ValueError(f"Failed to persist model profile: {name}")
    return profile


def delete_workspace_model_profile(workspace: FileWorkspace | Path, name: str) -> dict[str, Any]:
    ws = workspace if isinstance(workspace, FileWorkspace) else FileWorkspace(Path(workspace))
    ws.ensure()
    config_path = ws.workspace_config_path
    if not config_path.exists():
        raise ValueError(f"Workspace model config does not exist: {ws.relative_path(config_path)}")
    doc = parse(config_path.read_text(encoding="utf-8"))
    models_table = doc.get("models")
    if models_table is None or name not in models_table:
        raise ValueError(f"Unknown workspace model profile: {name}")
    del models_table[name]
    config_path.write_text(dumps(doc), encoding="utf-8")
    return {"deleted": True, "name": name}
