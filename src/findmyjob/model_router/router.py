from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("findmyjob.model_router")

_MODEL_TRACE_HANDLER: ContextVar[Any | None] = ContextVar("findmyjob_model_trace_handler", default=None)


def set_model_trace_handler(handler: Any) -> Token:
    return _MODEL_TRACE_HANDLER.set(handler)


def reset_model_trace_handler(token: Token) -> None:
    _MODEL_TRACE_HANDLER.reset(token)


def _emit_model_trace(payload: dict[str, Any]) -> None:
    handler = _MODEL_TRACE_HANDLER.get()
    if not callable(handler):
        return
    try:
        handler(dict(payload))
    except Exception:
        return

from findmyjob.core.config import AppConfig
from findmyjob.core.enums import ModelRole
from findmyjob.core.lmstudio import (
    LMSTUDIO_DEFAULT_BASE_URL,
    LMSTUDIO_DEFAULT_HOST,
    LMSTUDIO_DEFAULT_SCREENING_MODEL,
    LMSTUDIO_DEFAULT_WRITER_MODEL,
    LMSTUDIO_PROVIDER,
    canonical_lmstudio_base_url,
    lmstudio_available_model_ids,
    lmstudio_models_payload_is_catalog,
    resolve_lmstudio_model_id_or_default,
)
from findmyjob.core.types import ModelLaunchProfileReport, ModelLaunchRoleStatus, ModelProfile
from findmyjob.security.secrets import get_secret

_SCREENING_ROLES = {
    ModelRole.CLASSIFIER,
    ModelRole.EXTRACTOR,
    ModelRole.TEXT_ROUTER,
}
_WRITER_REASONING_ROLES = {
    ModelRole.WRITER,
    ModelRole.RESUME_WRITER,
    ModelRole.COVER_LETTER_WRITER,
    ModelRole.QUESTION_ANSWERER,
}
_MAX_LOCAL_COMPLETION_TOKENS = 16384
_NO_THINK_PATTERN = re.compile(r"^\s*/no[-_]think\b", re.IGNORECASE)
_LONG_RUNNING_WRITER_ROLES = {
    ModelRole.WRITER,
    ModelRole.RESUME_WRITER,
    ModelRole.COVER_LETTER_WRITER,
    ModelRole.QUESTION_ANSWERER,
}


class ModelRouter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._local_catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._local_catalog_cache_lock = threading.Lock()
        self._local_catalog_inflight: dict[str, asyncio.Future[dict[str, Any] | None]] = {}

    def required_roles(self) -> tuple[ModelRole, ...]:
        return (ModelRole.CLASSIFIER, ModelRole.WRITER, ModelRole.QUESTION_ANSWERER)

    def list_profiles(self) -> list[ModelProfile]:
        return list(self.config.models.values())

    def get_profile(self, name: str | None = None, role: ModelRole | None = None) -> ModelProfile:
        if name is not None:
            try:
                return self.config.models[name]
            except KeyError as exc:
                raise ValueError(f'Unknown model profile: {name}') from exc
        if role is not None:
            profile = self.config.model_for_role(role)
            if profile is None:
                raise ValueError(f'No model profile bound for role: {role.value}')
            return profile
        raise ValueError('Either name or role is required')

    def transport_mode(self, profile: ModelProfile) -> str:
        explicit = (profile.transport or '').strip().lower()
        if explicit in {'local_http', 'remote_http', 'process'}:
            return explicit
        if profile.command:
            return 'process'
        if profile.provider == LMSTUDIO_PROVIDER:
            return 'local_http'
        if profile.provider in ('llama_cpp', 'llamacpp', 'llama.cpp'):
            return 'local_http'
        if profile.provider == 'ollama' or profile.local:
            return 'local_http'
        return 'remote_http'

    def inspect_profiles(self) -> dict[str, Any]:
        role_bindings: dict[str, list[str]] = {}
        profiles: list[dict[str, Any]] = []
        for profile in self.list_profiles():
            role_bindings.setdefault(profile.role.value, []).append(profile.name)
            profiles.append(self._inspect_profile(profile))

        duplicate_roles = {role: names for role, names in role_bindings.items() if len(names) > 1}
        missing_required_roles = [role.value for role in self.required_roles() if self.config.model_for_role(role) is None]
        return {
            'required_roles': [role.value for role in self.required_roles()],
            'missing_required_roles': missing_required_roles,
            'duplicate_roles': duplicate_roles,
            'profiles': profiles,
        }

    def launch_profile_roles(self) -> tuple[ModelRole, ...]:
        return (ModelRole.CLASSIFIER, ModelRole.WRITER, ModelRole.QUESTION_ANSWERER)

    def inspect_launch_profile(self) -> ModelLaunchProfileReport:
        profile_inspection = self.inspect_profiles()
        by_role = {profile['role']: profile for profile in profile_inspection['profiles']}
        by_name = {profile['name']: profile for profile in profile_inspection['profiles']}
        required_roles = [role.value for role in self.launch_profile_roles()]
        optional_roles = [
            ModelRole.TEXT_ROUTER.value,
            ModelRole.EXTRACTOR.value,
            ModelRole.RESUME_WRITER.value,
            ModelRole.COVER_LETTER_WRITER.value,
        ]
        roles: list[ModelLaunchRoleStatus] = []
        risks: list[str] = []
        transports: set[str] = set()
        missing_required_roles: list[str] = []

        for role in [*required_roles, *optional_roles]:
            profile_data = by_role.get(role)
            if profile_data is None:
                if role in optional_roles:
                    roles.append(ModelLaunchRoleStatus(role=role, status='pass'))
                    continue
                missing_required_roles.append(role)
                roles.append(ModelLaunchRoleStatus(role=role, status='fail', issues=['role is not bound']))
                risks.append(f'{role} is not bound for the launch profile.')
                continue

            transports.add(profile_data['transport'])
            fallback_ready = [
                name
                for name in profile_data.get('fallback_chain') or []
                if by_name.get(name, {}).get('status') == 'ok'
            ]
            issues = list(profile_data.get('issues') or [])
            role_status = 'pass'
            if profile_data.get('status') == 'blocked':
                role_status = 'fail' if role in required_roles else 'warning'
                if role in required_roles:
                    risks.append(f"{role} profile `{profile_data.get('name')}` is not ready.")
            provider_name = str(profile_data.get('provider') or '').strip().lower()
            transport_name = str(profile_data.get('transport') or '').strip().lower()
            if provider_name != LMSTUDIO_PROVIDER:
                issues.append('launch contract requires LM Studio-local provider bindings')
                if role in required_roles:
                    role_status = 'fail'
                    risks.append(f"{role} profile `{profile_data.get('name')}` is not using LM Studio.")
                else:
                    role_status = 'warning'
            if transport_name != 'local_http':
                issues.append('launch contract requires LM Studio local_http transport')
                if role in required_roles:
                    role_status = 'fail'
                    risks.append(f"{role} profile `{profile_data.get('name')}` is not using local_http transport.")
                else:
                    role_status = 'warning'

            roles.append(
                ModelLaunchRoleStatus(
                    role=role,
                    profile_name=profile_data.get('name'),
                    transport=profile_data.get('transport'),
                    provider=profile_data.get('provider'),
                    model=profile_data.get('model'),
                    fallback_chain=list(profile_data.get('fallback_chain') or []),
                    fallback_ready=fallback_ready,
                    status=role_status,
                    issues=issues,
                )
            )

        if not transports:
            transport_mix = 'unbound'
        elif transports == {'local_http'}:
            transport_mix = 'all_local'
        elif transports == {'remote_http'}:
            transport_mix = 'all_remote'
        else:
            transport_mix = 'mixed'

        required_ready = len([item for item in roles if item.role in required_roles and item.status == 'pass'])
        configured_optional = [item for item in roles if item.role in optional_roles and item.profile_name]
        optional_ready = len([item for item in configured_optional if item.status == 'pass'])
        summary = f'{required_ready}/{len(required_roles)} required launch roles ready'
        if configured_optional:
            summary = f'{summary} | optional {optional_ready}/{len(configured_optional)} ready'

        return ModelLaunchProfileReport(
            required_roles=required_roles,
            optional_roles=optional_roles,
            roles=roles,
            missing_required_roles=missing_required_roles,
            transport_mix=transport_mix,
            risks=list(dict.fromkeys(risks)),
            summary=summary,
        )

    def resolve_base_url(self, profile: ModelProfile) -> str | None:
        if profile.base_url:
            if profile.provider == LMSTUDIO_PROVIDER:
                return canonical_lmstudio_base_url(profile.base_url)
            return profile.base_url
        for env_name in self._base_url_env_names(profile):
            value = os.environ.get(env_name)
            if value:
                if profile.provider == LMSTUDIO_PROVIDER:
                    return canonical_lmstudio_base_url(value)
                return value
        return None

    @staticmethod
    def _default_local_base_url(profile: ModelProfile | dict[str, Any]) -> str:
        """Return the default HTTP base URL for a local model profile."""
        if isinstance(profile, dict):
            provider = (profile.get('provider') or '').strip().lower()
        else:
            provider = (profile.provider or '').strip().lower()
        if provider == LMSTUDIO_PROVIDER:
            return LMSTUDIO_DEFAULT_BASE_URL
        if provider in ('llama_cpp', 'llamacpp', 'llama.cpp'):
            return 'http://127.0.0.1:8080/v1'
        return 'http://127.0.0.1:11434/v1'

    def secret_status(self, profile: ModelProfile) -> dict[str, Any]:
        transport = self.transport_mode(profile)
        if transport in {'local_http', 'process'}:
            return {
                'required': False,
                'configured': None,
                'satisfied': True,
                'source': 'not_required',
                'reference': profile.api_key_env,
            }

        if not profile.api_key_env:
            return {
                'required': True,
                'configured': False,
                'satisfied': False,
                'source': 'missing',
                'reference': None,
            }

        env_value = os.environ.get(profile.api_key_env)
        if env_value:
            return {
                'required': True,
                'configured': True,
                'satisfied': True,
                'source': 'env',
                'reference': profile.api_key_env,
            }

        keyring_secret = get_secret(profile.api_key_env) or get_secret(profile.name)
        if keyring_secret:
            return {
                'required': True,
                'configured': True,


                'satisfied': True,
                'source': 'keyring',
                'reference': profile.api_key_env,
            }

        return {
            'required': True,
            'configured': True,
            'satisfied': False,
            'source': 'missing',
            'reference': profile.api_key_env,
        }

    async def _fetch_local_model_catalog(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any] | None:
        cache_key = f"{base_url.rstrip('/')}|{api_key or ''}"
        now = time.monotonic()
        cached: tuple[float, dict[str, Any]] | None = None
        wait_for: asyncio.Future[dict[str, Any] | None] | None = None
        owner = False
        with self._local_catalog_cache_lock:
            cached = self._local_catalog_cache.get(cache_key)
            if not refresh and cached is not None and (now - cached[0]) < 30.0:
                return cached[1]
            wait_for = self._local_catalog_inflight.get(cache_key)
            if wait_for is None:
                wait_for = asyncio.get_running_loop().create_future()
                self._local_catalog_inflight[cache_key] = wait_for
                owner = True
        if not owner:
            return await wait_for
        headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        payload: dict[str, Any] | None = None
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
                response = await client.get(f"{base_url.rstrip('/')}/models")
                response.raise_for_status()
                payload = response.json()
            if not lmstudio_models_payload_is_catalog(payload):
                raise RuntimeError(f"Unexpected model catalog payload from {base_url.rstrip('/')}/models")
        except Exception:
            payload = cached[1] if cached is not None else None
        with self._local_catalog_cache_lock:
            if payload is not None:
                self._local_catalog_cache[cache_key] = (time.monotonic(), payload)
            pending = self._local_catalog_inflight.pop(cache_key, None)
        if pending is not None and not pending.done():
            pending.set_result(payload)
        return payload

    async def _resolved_local_model_name(self, profile: ModelProfile, base_url: str) -> str:
        catalog = await self._fetch_local_model_catalog(
            base_url,
            api_key=self._api_key_for_profile(profile),
        )
        if catalog is None:
            return profile.model
        return resolve_lmstudio_model_id_or_default(profile.model, catalog) or profile.model

    async def discover_local_http_models(self, base_url: str | None = None) -> list[str]:
        target_base_url = canonical_lmstudio_base_url(base_url or LMSTUDIO_DEFAULT_HOST)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{target_base_url.rstrip('/')}/models")
            response.raise_for_status()
            payload = response.json()
        return lmstudio_available_model_ids(payload)

    async def test_profile(self, profile: ModelProfile) -> dict[str, Any]:
        inspection = self._inspect_profile(profile)
        if inspection['status'] == 'blocked':
            return {
                'profile': profile.name,
                'role': profile.role.value,
                'provider': profile.provider,
                'model': profile.model,
                'base_url': self.resolve_base_url(profile),
                'transport': inspection['transport'],
                'ok': False,
                'classification': 'misconfigured',
                'issues': inspection['issues'],
                'fallback_chain': list(profile.fallback_chain),
                'policy_tags': list(profile.policy_tags),
            }

        t0 = time.monotonic()
        try:
            transport = self.transport_mode(profile)
            if transport == 'process':
                content = await self._process_completion(
                    profile,
                    role=profile.role,
                    mode='text',
                    prompt='Reply with the single word ready.',
                    system_prompt='You are a model health-check assistant. Reply with the single word ready.',
                )
                return {
                    'profile': profile.name,
                    'role': profile.role.value,
                    'provider': profile.provider,
                    'model': profile.model,
                    'base_url': self.resolve_base_url(profile),
                    'transport': inspection['transport'],
                    'ok': str(content or '').strip().lower().startswith('ready'),
                    'classification': 'ok' if str(content or '').strip().lower().startswith('ready') else 'unexpected_response',
                    'details': {'content': str(content or '').strip()},
                    'latency_ms': round((time.monotonic() - t0) * 1000),
                    'fallback_chain': list(profile.fallback_chain),
                    'policy_tags': list(profile.policy_tags),
                }
            payload = self._base_payload(
                profile,
                'Reply with the single word ready.',
                'You are a model health-check assistant. Reply with the single word ready.',
                role=profile.role,
            )
            response = await self._chat_completion(profile, payload, mode='text')
            content = self._extract_chat_content(response, profile_name=profile.name).strip()
            catalog: dict[str, Any] | None = None
            available_models: list[str] = []
            model_present: bool | None = None
            base_url = self.resolve_base_url(profile) or (self._default_local_base_url(profile) if transport == 'local_http' else None)
            if base_url:
                try:
                    catalog = await self._fetch_local_model_catalog(
                        base_url,
                        api_key=self._api_key_for_profile(profile),
                        refresh=True,
                    )
                    if catalog is not None:
                        available_models = lmstudio_available_model_ids(catalog)
                        model_present = resolve_lmstudio_model_id_or_default(profile.model, catalog) is not None
                except Exception:
                    catalog = None
            ok = content.lower().startswith('ready')
            return {
                'profile': profile.name,
                'role': profile.role.value,
                'provider': profile.provider,
                'model': profile.model,
                'base_url': base_url,
                'transport': inspection['transport'],
                'ok': ok,
                'classification': 'ok' if ok else 'unexpected_response',
                'details': {
                    'content': content,
                    'catalog': catalog,
                },
                'available_models': available_models,
                'model_present': model_present,
                'latency_ms': round((time.monotonic() - t0) * 1000),
                'fallback_chain': list(profile.fallback_chain),
                'policy_tags': list(profile.policy_tags),
            }
        except Exception as exc:
            return {
                'profile': profile.name,
                'role': profile.role.value,
                'provider': profile.provider,
                'model': profile.model,
                'base_url': self.resolve_base_url(profile),
                'transport': inspection['transport'],
                'ok': False,
                'classification': 'unreachable',
                'error': str(exc),
                'latency_ms': round((time.monotonic() - t0) * 1000),
                'fallback_chain': list(profile.fallback_chain),
                'policy_tags': list(profile.policy_tags),
            }

    async def auto_detect_profiles(self) -> dict[str, Any]:
        try:
            models = await self.discover_local_http_models()
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'profiles': []}

        writer_model = LMSTUDIO_DEFAULT_WRITER_MODEL
        screening_model = LMSTUDIO_DEFAULT_SCREENING_MODEL
        vlm_model = LMSTUDIO_DEFAULT_WRITER_MODEL
        if writer_model not in models and models:
            writer_model = models[0]
        if screening_model not in models and models:
            screening_model = models[0]
        if vlm_model not in models and models:
            vlm_model = models[0]

        profiles: list[dict[str, Any]] = []

        role_map = {
            'text-router': ModelRole.TEXT_ROUTER,
            'writer': ModelRole.WRITER,
            'extractor': ModelRole.EXTRACTOR,
            'classifier': ModelRole.CLASSIFIER,
            'resume-writer': ModelRole.RESUME_WRITER,
            'cover-letter-writer': ModelRole.COVER_LETTER_WRITER,
            'question-answerer': ModelRole.QUESTION_ANSWERER,
            'page-vlm': ModelRole.PAGE_VLM,
            'page-vlm-verifier': ModelRole.PAGE_VLM_VERIFIER,
        }

        for name, role in role_map.items():
            if role in {ModelRole.TEXT_ROUTER, ModelRole.EXTRACTOR, ModelRole.CLASSIFIER}:
                selected = screening_model
            else:
                selected = vlm_model if role in {ModelRole.PAGE_VLM, ModelRole.PAGE_VLM_VERIFIER} else writer_model
            if selected is None:
                continue
            profiles.append(
                {
                    'name': f"lmstudio-{name}",
                    'role': role.value,
                    'provider': LMSTUDIO_PROVIDER,
                    'model': selected,
                    'local': True,
                    'transport': 'local_http',
                    'temperature': 0.0 if role in {ModelRole.EXTRACTOR, ModelRole.CLASSIFIER, ModelRole.PAGE_VLM_VERIFIER, ModelRole.TEXT_ROUTER} else 0.7,
                    'supports_structured_output': role in {ModelRole.EXTRACTOR, ModelRole.CLASSIFIER, ModelRole.PAGE_VLM_VERIFIER},
                    'base_url': LMSTUDIO_DEFAULT_BASE_URL,
                    'api_key_env': None,
                }
            )

        return {'ok': True, 'available_models': models, 'profiles': profiles}

    async def generate_text(self, role: ModelRole, prompt: str, system_prompt: str | None = None) -> str:
        text, _profile_name = await self.generate_text_with_profile(role, prompt, system_prompt=system_prompt)
        return text

    async def generate_text_with_profile(self, role: ModelRole, prompt: str, system_prompt: str | None = None) -> tuple[str, str]:
        profile = self.get_profile(role=role)
        content, used_profile = await self._generate_with_fallback(profile, role=role, mode='text', prompt=prompt, system_prompt=system_prompt or 'You are a precise assistant.')
        return str(content), used_profile.name

    async def generate_json(
        self,
        role: ModelRole,
        prompt: str,
        system_prompt: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload, _profile_name = await self.generate_json_with_profile(
            role,
            prompt,
            system_prompt=system_prompt,
            json_schema=json_schema,
        )
        return payload

    async def generate_json_with_profile(
        self,
        role: ModelRole,
        prompt: str,
        system_prompt: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        profile = self.get_profile(role=role)
        payload, used_profile = await self._generate_with_fallback(
            profile,
            role=role,
            mode='json',
            prompt=prompt,
            system_prompt=system_prompt or 'Return only valid JSON.',
            json_schema=json_schema,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f'Profile `{used_profile.name}` returned non-object JSON payload.')
        return payload, used_profile.name

    def _parse_json_content(self, content: Any) -> Any:
        if isinstance(content, (dict, list)):
            return content
        text = str(content or '').strip()
        if not text:
            raise RuntimeError('Model returned an empty JSON response.')
        candidates: list[str] = [text]
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if fence_match:
            candidates.append(fence_match.group(1).strip())
        object_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if object_match:
            candidates.append(object_match.group(1).strip())

        decoder = json.JSONDecoder()
        last_error: json.JSONDecodeError | None = None
        seen: set[str] = set()
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_error = exc
            for start_char in ('{', '['):
                start_index = candidate.find(start_char)
                if start_index < 0:
                    continue
                snippet = candidate[start_index:].strip()
                if not snippet or snippet in seen:
                    continue
                seen.add(snippet)
                try:
                    parsed, _end = decoder.raw_decode(snippet)
                    if isinstance(parsed, (dict, list)):
                        return parsed
                except json.JSONDecodeError as exc:
                    last_error = exc
        detail = f'{type(last_error).__name__}: {last_error}' if last_error is not None else 'no JSON object found'
        raise RuntimeError(f'invalid JSON response: {detail}')

    @staticmethod
    def _coerce_message_content(content: Any) -> str:
        if content is None:
            return ''
        if isinstance(content, str):
            return content
        if isinstance(content, (int, float, bool)):
            return str(content)
        if isinstance(content, dict):
            if content.get('text') is not None:
                return str(content.get('text'))
            if content.get('content') is not None:
                return str(content.get('content'))
            return str(content)
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if item is None:
                    continue
                if isinstance(item, dict):
                    if item.get('text') is not None:
                        parts.append(str(item.get('text')))
                        continue
                    if item.get('content') is not None:
                        parts.append(str(item.get('content')))
                        continue
                parts.append(str(item))
            return ''.join(parts).strip()
        return str(content)

    @staticmethod
    def _provider_error_detail(response: dict[str, Any]) -> str | None:
        error_payload = response.get('error')
        if error_payload in (None, ''):
            return None
        if isinstance(error_payload, str):
            return error_payload.strip() or None
        if not isinstance(error_payload, dict):
            return str(error_payload)
        message = str(
            error_payload.get('message')
            or error_payload.get('detail')
            or error_payload.get('type')
            or error_payload
        ).strip()
        extras: list[str] = []
        code = error_payload.get('code')
        if code not in (None, ''):
            extras.append(f'code={code}')
        err_type = error_payload.get('type')
        if err_type not in (None, ''):
            extras.append(f'type={err_type}')
        if extras:
            return f"{message} ({', '.join(extras)})"
        return message or None

    @staticmethod
    def _http_error_detail(response: httpx.Response) -> str:
        payload: Any | None = None
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = ModelRouter._provider_error_detail(payload)
            if detail:
                return detail
            compact = json.dumps(payload, ensure_ascii=True)
            return compact if len(compact) <= 400 else compact[:397] + '...'
        text = str(response.text or '').strip()
        if text:
            return text if len(text) <= 400 else text[:397] + '...'
        return str(response.reason_phrase or 'no response body')

    def _extract_chat_content(self, response: Any, *, profile_name: str) -> str:
        if not isinstance(response, dict):
            raise RuntimeError(
                f'Profile `{profile_name}` returned non-object chat payload ({type(response).__name__}).'
            )
        provider_error = self._provider_error_detail(response)
        if provider_error:
            raise RuntimeError(
                f'Provider returned error payload for `{profile_name}`: {provider_error}'
            )
        choices = response.get('choices')
        if not isinstance(choices, list) or not choices:
            keys = ', '.join(sorted(response.keys()))
            raise RuntimeError(
                f'Profile `{profile_name}` returned payload without `choices` (keys: {keys or "none"}).'
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise RuntimeError(f'Profile `{profile_name}` returned invalid `choices[0]` payload.')
        message = first_choice.get('message')
        if isinstance(message, dict):
            content = self._strip_thinking_tags(self._coerce_message_content(message.get('content')))
            if content.strip():
                return content
            reasoning_content = self._strip_thinking_tags(self._coerce_message_content(message.get('reasoning_content')))
            if reasoning_content.strip():
                return reasoning_content
        if first_choice.get('text') is not None:
            return self._strip_thinking_tags(str(first_choice.get('text')))
        delta = first_choice.get('delta')
        if isinstance(delta, dict):
            content = self._strip_thinking_tags(self._coerce_message_content(delta.get('content')))
            if content.strip():
                return content
            reasoning_content = self._strip_thinking_tags(self._coerce_message_content(delta.get('reasoning_content')))
            if reasoning_content.strip():
                return reasoning_content
        raise RuntimeError(
            f'Profile `{profile_name}` returned `choices` without usable text content.'
        )

    @staticmethod
    def _strip_thinking_tags(text: Any) -> str:
        cleaned = str(text or '')
        cleaned = re.sub(r'<think>[\s\S]*?</think>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'</?think>', '', cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _base_payload(
        self,
        profile: ModelProfile,
        prompt: str,
        system_prompt: str,
        *,
        role: ModelRole,
    ) -> dict[str, Any]:
        payload = {
            'model': profile.model,
            'temperature': profile.temperature,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': prompt},
            ],
        }
        if profile.max_tokens is not None:
            payload['max_tokens'] = profile.max_tokens
        disable_thinking = role in _SCREENING_ROLES and profile.temperature == 0.0
        if not disable_thinking and (
            _NO_THINK_PATTERN.search(str(prompt or "")) is not None
            or _NO_THINK_PATTERN.search(str(system_prompt or "")) is not None
        ):
            disable_thinking = True
        if disable_thinking:
            payload['chat_template_kwargs'] = {'enable_thinking': False}
        if role in _WRITER_REASONING_ROLES:
            payload['max_completion_tokens'] = min(
                int(profile.max_tokens or _MAX_LOCAL_COMPLETION_TOKENS),
                _MAX_LOCAL_COMPLETION_TOKENS,
            )
        return payload

    async def _generate_with_fallback(
        self,
        profile: ModelProfile,
        *,
        role: ModelRole,
        mode: str,
        prompt: str,
        system_prompt: str,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[Any, ModelProfile]:
        errors: list[str] = []
        profile_chain = self._profile_chain(profile)
        chain_names = [candidate.name for candidate in profile_chain]
        prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        for attempt_index, candidate in enumerate(profile_chain, start=1):
            transport = self.transport_mode(candidate)
            call_id = uuid.uuid4().hex[:12]
            started_at = time.time()
            t0 = time.monotonic()
            _emit_model_trace(
                {
                    'call_id': call_id,
                    'status': 'started',
                    'started_at': started_at,
                    'role': role.value,
                    'mode': mode,
                    'profile_name': candidate.name,
                    'provider': candidate.provider,
                    'model': candidate.model,
                    'transport': transport,
                    'attempt': attempt_index,
                    'chain': chain_names,
                    'fallback_from': profile.name,
                    'prompt_hash': prompt_hash,
                    'max_tokens': candidate.max_tokens,
                    'temperature': candidate.temperature,
                }
            )
            log.info("      [model] Calling %s (role=%s, model=%s, transport=%s)...",
                     candidate.name, role.value, candidate.model, transport)
            try:
                if transport == 'process':
                    payload = await self._process_completion(
                        candidate,
                        role=role,
                        mode=mode,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        json_schema=json_schema,
                    )
                    elapsed = time.monotonic() - t0
                    elapsed_ms = round(elapsed * 1000)
                    log.info("      [model] %s responded in %.1fs (process)", candidate.name, elapsed)
                    trace_payload = {
                        'call_id': call_id,
                        'status': 'completed',
                        'started_at': started_at,
                        'completed_at': time.time(),
                        'role': role.value,
                        'mode': mode,
                        'profile_name': candidate.name,
                        'provider': candidate.provider,
                        'model': candidate.model,
                        'transport': transport,
                        'attempt': attempt_index,
                        'chain': chain_names,
                        'fallback_from': profile.name,
                        'prompt_hash': prompt_hash,
                        'latency_ms': elapsed_ms,
                        'usage': {},
                        'response_kind': 'json_object' if mode == 'json' else 'text',
                    }
                    if mode == 'json':
                        if isinstance(payload, dict):
                            trace_payload['parsed_output_kind'] = 'object'
                            trace_payload['parsed_output_keys'] = sorted(str(key) for key in payload.keys())[:20]
                        elif isinstance(payload, list):
                            trace_payload['parsed_output_kind'] = 'array'
                            trace_payload['parsed_output_count'] = len(payload)
                        else:
                            trace_payload['parsed_output_kind'] = type(payload).__name__
                        _emit_model_trace(trace_payload)
                        return payload, candidate
                    _emit_model_trace(trace_payload)
                    return str(payload), candidate
                request_payload = self._base_payload(candidate, prompt, system_prompt, role=role)
                if json_schema is None:
                    response = await self._chat_completion(candidate, request_payload, mode=mode)
                else:
                    response = await self._chat_completion(candidate, request_payload, mode=mode, json_schema=json_schema)
                elapsed = time.monotonic() - t0
                elapsed_ms = round(elapsed * 1000)
                log.info("      [model] %s responded in %.1fs", candidate.name, elapsed)
                content = self._extract_chat_content(response, profile_name=candidate.name)
                usage = dict(response.get('usage') or {})
                trace_payload = {
                    'call_id': call_id,
                    'status': 'completed',
                    'started_at': started_at,
                    'completed_at': time.time(),
                    'role': role.value,
                    'mode': mode,
                    'profile_name': candidate.name,
                    'provider': candidate.provider,
                    'model': response.get('model') or candidate.model,
                    'transport': transport,
                    'attempt': attempt_index,
                    'chain': chain_names,
                    'fallback_from': profile.name,
                    'prompt_hash': prompt_hash,
                    'latency_ms': elapsed_ms,
                    'usage': usage,
                    'response_kind': 'json_object' if mode == 'json' else 'text',
                    'response_chars': len(content),
                }
                if mode == 'json':
                    parsed = self._parse_json_content(content)
                    if isinstance(parsed, dict):
                        trace_payload['parsed_output_kind'] = 'object'
                        trace_payload['parsed_output_keys'] = sorted(str(key) for key in parsed.keys())[:20]
                    elif isinstance(parsed, list):
                        trace_payload['parsed_output_kind'] = 'array'
                        trace_payload['parsed_output_count'] = len(parsed)
                    else:
                        trace_payload['parsed_output_kind'] = type(parsed).__name__
                    _emit_model_trace(trace_payload)
                    return parsed, candidate
                _emit_model_trace(trace_payload)
                return content, candidate
            except Exception as exc:
                elapsed = time.monotonic() - t0
                elapsed_ms = round(elapsed * 1000)
                log.warning("      [model] %s FAILED after %.1fs: %s", candidate.name, elapsed, exc)
                _emit_model_trace(
                    {
                        'call_id': call_id,
                        'status': 'failed',
                        'started_at': started_at,
                        'completed_at': time.time(),
                        'role': role.value,
                        'mode': mode,
                        'profile_name': candidate.name,
                        'provider': candidate.provider,
                        'model': candidate.model,
                        'transport': transport,
                        'attempt': attempt_index,
                        'chain': chain_names,
                        'fallback_from': profile.name,
                        'prompt_hash': prompt_hash,
                        'latency_ms': elapsed_ms,
                        'error': {'message': str(exc), 'type': type(exc).__name__},
                    }
                )
                errors.append(f'{candidate.name}: {exc}')
        raise RuntimeError('; '.join(errors) if errors else f'No usable model profile for {profile.name}')

    def _profile_chain(self, profile: ModelProfile) -> list[ModelProfile]:
        chain: list[ModelProfile] = []
        seen: set[str] = set()
        for candidate in [profile, *(self.config.models.get(name) for name in profile.fallback_chain)]:
            if candidate is None or candidate.name in seen:
                continue
            seen.add(candidate.name)
            chain.append(candidate)
        return chain

    def _api_key_for_profile(self, profile: ModelProfile) -> str | None:
        if profile.api_key_env:
            env_value = os.environ.get(profile.api_key_env)
            if env_value:
                return env_value
            secret = get_secret(profile.api_key_env)
            if secret:
                return secret
        secret = get_secret(profile.name)
        if secret:
            return secret
        return None

    async def _chat_completion(
        self,
        profile: ModelProfile,
        payload: dict[str, Any],
        *,
        mode: str,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transport = self.transport_mode(profile)
        if transport == 'local_http':
            base_url = self.resolve_base_url(profile) or self._default_local_base_url(profile)
            retry_cfg = self.config.retry
            return await self._local_chat_with_retry(
                base_url, payload, mode=mode, profile=profile, json_schema=json_schema,
                max_retries=retry_cfg.max_retries,
                backoff_base=retry_cfg.backoff_base,
                backoff_max=retry_cfg.backoff_max,
            )

        base_url = self.resolve_base_url(profile)
        if not base_url:
            raise RuntimeError(f'Remote provider `{profile.provider}` for `{profile.name}` is missing a base URL.')
        api_key = self._api_key_for_profile(profile)
        if profile.api_key_env and not api_key:
            raise RuntimeError(f'Remote provider `{profile.provider}` for `{profile.name}` is missing secret `{profile.api_key_env}`.')
        headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
        timeout = httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0)

        retry_cfg = self.config.retry
        max_retries = retry_cfg.max_retries
        backoff_base = retry_cfg.backoff_base
        backoff_max = retry_cfg.backoff_max
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                    response = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        json=self._http_payload(payload, mode=mode, profile=profile, json_schema=json_schema),
                    )
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code
                detail = self._http_error_detail(exc.response)
                if status_code in {429, 500, 502, 503, 504}:
                    log.warning(
                        "      [model] Remote HTTP %d (attempt %d/%d): %s",
                        status_code,
                        attempt,
                        max_retries,
                        detail,
                    )
                    if attempt < max_retries:
                        wait = min(backoff_base ** attempt, backoff_max)
                        log.info("      [model] Retrying remote call in %.1fs...", wait)
                        await asyncio.sleep(wait)
                        continue
                raise RuntimeError(
                    f"Remote HTTP {status_code} from `{profile.name}`: {detail}"
                ) from exc
            except (httpx.ConnectError, httpx.TimeoutException, TimeoutError) as exc:
                last_error = exc
                log.warning("      [model] Remote connection failed (attempt %d/%d): %s",
                            attempt, max_retries, exc)
                if attempt < max_retries:
                    wait = min(backoff_base ** attempt, backoff_max)
                    await asyncio.sleep(wait)
                    continue

        raise RuntimeError(
            f"Remote call to `{profile.name}` failed after {max_retries} attempts: {last_error}"
        ) from last_error

    async def _local_chat_with_retry(
        self,
        base_url: str,
        payload: dict[str, Any],
        *,
        mode: str,
        profile: ModelProfile,
        json_schema: dict[str, Any] | None = None,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
    ) -> dict[str, Any]:
        """Call local LLM with retry and exponential backoff for transient failures."""
        last_error: Exception | None = None
        effective_max_retries = 1 if profile.role in _LONG_RUNNING_WRITER_ROLES else max_retries
        read_timeout = 900.0 if profile.role in _LONG_RUNNING_WRITER_ROLES else 180.0
        request_payload = dict(payload)
        resolved_model = await self._resolved_local_model_name(profile, base_url)
        if resolved_model:
            request_payload['model'] = resolved_model
        for attempt in range(1, effective_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=read_timeout, write=30.0, pool=30.0)) as client:
                    http_payload = self._http_payload(
                        request_payload,
                        mode=mode,
                        profile=profile,
                        json_schema=json_schema,
                    )
                    response = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        json=http_payload,
                    )
                    if mode == 'json' and response.status_code >= 400 and 'response_format' in http_payload:
                        fallback_payload = dict(http_payload)
                        fallback_payload.pop('response_format', None)
                        response = await client.post(
                            f"{base_url.rstrip('/')}/chat/completions",
                            json=fallback_payload,
                        )
                    response.raise_for_status()
                    return response.json()
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                log.warning("      [model] Local LLM connection failed (attempt %d/%d): %s", attempt, effective_max_retries, exc)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code in {429, 500, 502, 503, 504}:
                    log.warning("      [model] Local LLM HTTP %d (attempt %d/%d)", exc.response.status_code, attempt, effective_max_retries)
                else:
                    raise
            if attempt < effective_max_retries:
                wait = min(backoff_base ** attempt, backoff_max)
                log.info("      [model] Retrying local LLM in %.1fs...", wait)
                await asyncio.sleep(wait)
        raise RuntimeError(
            f"Local model server at {base_url} unavailable after {effective_max_retries} attempts: {last_error}"
        )

    async def health_check_local(self) -> dict[str, Any]:
        """Check health of all local model profiles."""
        results: dict[str, Any] = {}
        for profile in self.list_profiles():
            if self.transport_mode(profile) != 'local_http':
                continue
            base_url = self.resolve_base_url(profile) or self._default_local_base_url(profile)
            try:
                catalog = await self._fetch_local_model_catalog(base_url, api_key=self._api_key_for_profile(profile), refresh=True)
                if catalog is None:
                    raise RuntimeError(f'Unable to load model catalog from {base_url}')
                model_ids = lmstudio_available_model_ids(catalog)
                results[profile.name] = {
                    'healthy': True,
                    'model_present': resolve_lmstudio_model_id_or_default(profile.model, catalog) is not None,
                    'available_models': model_ids,
                }
            except Exception as exc:
                results[profile.name] = {
                    'healthy': False,
                    'error': str(exc),
                    'error_type': type(exc).__name__,
                }
        return results

    def _http_payload(
        self,
        payload: dict[str, Any],
        *,
        mode: str,
        profile: ModelProfile,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_payload = dict(payload)
        if mode == 'json' and profile.supports_structured_output:
            transport = self.transport_mode(profile)
            if transport == 'local_http':
                # LM Studio requires json_schema format, not json_object.
                schema = json_schema if isinstance(json_schema, dict) and json_schema else {'type': 'object'}
                request_payload['response_format'] = {
                    'type': 'json_schema',
                    'json_schema': {
                        'name': 'response',
                        'strict': False,
                        'schema': schema,
                    },
                }
            else:
                request_payload['response_format'] = {'type': 'json_object'}
        return request_payload

    async def _process_completion(
        self,
        profile: ModelProfile,
        *,
        role: ModelRole,
        mode: str,
        prompt: str,
        system_prompt: str,
        json_schema: dict[str, Any] | None = None,
    ) -> Any:
        if not profile.command:
            raise RuntimeError(f'Process model profile `{profile.name}` is missing a command.')
        request = {
            'role': role.value,
            'mode': mode,
            'system_prompt': system_prompt,
            'prompt': prompt,
            'model': profile.model,
            'temperature': profile.temperature,
            'max_tokens': profile.max_tokens,
            'json_schema': json_schema,
        }
        process = await asyncio.create_subprocess_exec(
            *profile.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=profile.working_dir or None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode('utf-8')),
                timeout=120.0,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RuntimeError(f'process profile `{profile.name}` timed out') from exc

        if process.returncode != 0:
            raise RuntimeError((stderr or stdout).decode('utf-8', errors='ignore').strip() or f'process exited {process.returncode}')
        try:
            payload = json.loads(stdout.decode('utf-8'))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'process profile `{profile.name}` returned invalid JSON') from exc
        if not payload.get('ok'):
            raise RuntimeError(str(payload.get('error') or f'process profile `{profile.name}` returned failure'))
        if mode == 'json':
            result = payload.get('json')
            if result is None:
                raise RuntimeError(f'process profile `{profile.name}` returned no JSON payload')
            return result
        result = payload.get('content')
        if result is None:
            raise RuntimeError(f'process profile `{profile.name}` returned no text content')
        return result

    def _inspect_profile(self, profile: ModelProfile) -> dict[str, Any]:
        issues: list[str] = []
        transport_mode = self.transport_mode(profile)
        base_url = self.resolve_base_url(profile)
        secret = self.secret_status(profile)
        missing_fallbacks = [name for name in profile.fallback_chain if name not in self.config.models]
        if transport_mode not in {'local_http', 'remote_http', 'process'}:
            issues.append(f'unsupported transport `{transport_mode}`')
        if profile.name in profile.fallback_chain:
            issues.append('fallback chain includes the profile itself')
        if missing_fallbacks:
            issues.append(f"missing fallbacks: {', '.join(missing_fallbacks)}")
        if transport_mode == 'process':
            if not profile.command:
                issues.append('missing process command')
            elif not shutil.which(str(profile.command[0])) and not Path(str(profile.command[0])).exists():
                issues.append(f'process executable not found: {profile.command[0]}')
        elif transport_mode == 'remote_http':
            if not base_url:
                issues.append('missing base URL')
            if not profile.api_key_env:
                issues.append('missing api_key_env')
            elif not secret['satisfied']:
                issues.append(f"secret `{profile.api_key_env}` not found in env or keyring")
        status = 'blocked' if issues else 'ok'
        rendered_transport = transport_mode
        return {
            'name': profile.name,
            'role': profile.role.value,
            'provider': profile.provider,
            'model': profile.model,
            'local': profile.local,
            'transport': rendered_transport,
            'transport_mode': transport_mode,
            'base_url': base_url,
            'base_url_source': self._base_url_source(profile),
            'secret_reference': profile.api_key_env,
            'secret_satisfied': secret['satisfied'],
            'secret_source': secret['source'],
            'fallback_chain': list(profile.fallback_chain),
            'missing_fallbacks': missing_fallbacks,
            'policy_tags': list(profile.policy_tags),
            'status': status,
            'issues': issues,
            'command': list(profile.command),
            'working_dir': profile.working_dir,
        }

    def _base_url_env_names(self, profile: ModelProfile) -> list[str]:
        normalized = re.sub(r'[^A-Za-z0-9]+', '_', profile.name).upper()
        legacy = profile.name.upper()
        names = [f'{normalized}_BASE_URL']
        if legacy != normalized:
            names.append(f'{legacy}_BASE_URL')
        return names

    def _base_url_source(self, profile: ModelProfile) -> str | None:
        if profile.base_url:
            return 'config'
        for env_name in self._base_url_env_names(profile):
            if os.environ.get(env_name):
                return f'env:{env_name}'
        return None



