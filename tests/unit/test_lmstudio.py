from __future__ import annotations

import httpx

from findmyjob.core.config import AppConfig
from findmyjob.core.lmstudio import (
    LMSTUDIO_DEFAULT_HOST,
    LMSTUDIO_DEFAULT_SCREENING_MODEL,
    LMSTUDIO_DEFAULT_WRITER_MODEL,
    ResolvedLocalBaseURL,
    canonical_lmstudio_base_url,
    lmstudio_probe_candidates,
    probe_lmstudio_base_url,
    resolve_lmstudio_model_id,
)
from findmyjob.filefirst.advanced_models import _split_model_defaults_cached, install_recommended_split_profiles, split_model_defaults
from findmyjob.filefirst.workspace import FileWorkspace


def test_lmstudio_url_helpers_normalize_raw_host_and_v1() -> None:
    assert canonical_lmstudio_base_url(LMSTUDIO_DEFAULT_HOST) == f'{LMSTUDIO_DEFAULT_HOST}/v1'
    assert canonical_lmstudio_base_url(f'{LMSTUDIO_DEFAULT_HOST}/v1') == f'{LMSTUDIO_DEFAULT_HOST}/v1'
    assert lmstudio_probe_candidates(LMSTUDIO_DEFAULT_HOST) == (
        LMSTUDIO_DEFAULT_HOST,
        f'{LMSTUDIO_DEFAULT_HOST}/v1',
    )
    assert lmstudio_probe_candidates(f'{LMSTUDIO_DEFAULT_HOST}/v1') == (
        LMSTUDIO_DEFAULT_HOST,
        f'{LMSTUDIO_DEFAULT_HOST}/v1',
    )


def test_probe_lmstudio_base_url_tries_raw_host_then_v1(monkeypatch) -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, payload: dict[str, object], *, ok: bool = True) -> None:
            self._payload = payload
            self._ok = ok

        def raise_for_status(self) -> None:
            if not self._ok:
                raise httpx.HTTPStatusError('failed', request=httpx.Request('GET', 'http://testserver'), response=httpx.Response(500))

        def json(self) -> dict[str, object]:
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            _ = (args, kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = (exc_type, exc, tb)
            return None

        def get(self, url: str):
            calls.append(url)
            if url == f'{LMSTUDIO_DEFAULT_HOST}/models':
                raise httpx.ConnectError('refused', request=httpx.Request('GET', url))
            return _Response({'data': [{'id': LMSTUDIO_DEFAULT_WRITER_MODEL}]})

    monkeypatch.setattr('findmyjob.core.lmstudio.httpx.Client', _Client)

    resolved = probe_lmstudio_base_url(LMSTUDIO_DEFAULT_HOST)

    assert resolved.requested_url == LMSTUDIO_DEFAULT_HOST
    assert resolved.canonical_base_url == f'{LMSTUDIO_DEFAULT_HOST}/v1'
    assert calls == [
        f'{LMSTUDIO_DEFAULT_HOST}/models',
        f'{LMSTUDIO_DEFAULT_HOST}/v1/models',
    ]


def test_probe_lmstudio_base_url_skips_non_catalog_raw_payload(monkeypatch) -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            _ = (args, kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = (exc_type, exc, tb)
            return None

        def get(self, url: str):
            calls.append(url)
            if url == f'{LMSTUDIO_DEFAULT_HOST}/models':
                return _Response({'error': 'Unexpected endpoint or method. (GET /models)'})
            return _Response({'data': [{'id': 'qwen3-8b'}]})

    monkeypatch.setattr('findmyjob.core.lmstudio.httpx.Client', _Client)

    resolved = probe_lmstudio_base_url(LMSTUDIO_DEFAULT_HOST)

    assert resolved.canonical_base_url == f'{LMSTUDIO_DEFAULT_HOST}/v1'
    assert resolved.models_payload == {'data': [{'id': 'qwen3-8b'}]}
    assert calls == [
        f'{LMSTUDIO_DEFAULT_HOST}/models',
        f'{LMSTUDIO_DEFAULT_HOST}/v1/models',
    ]


def test_resolve_lmstudio_model_id_matches_normalized_catalog_ids() -> None:
    payload = {'data': [{'id': 'qwen3-8b'}, {'id': 'smollm3-3b'}]}

    assert resolve_lmstudio_model_id('lmstudio-community/Qwen3-8B-GGUF', payload) == 'qwen3-8b'
    assert resolve_lmstudio_model_id('lmstudio-community/SmolLM3-3B-GGUF', payload) == 'smollm3-3b'


def test_split_model_defaults_resolves_live_catalog_aliases(monkeypatch) -> None:
    _split_model_defaults_cached.cache_clear()

    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        requested = str(base_url or '').strip() or LMSTUDIO_DEFAULT_HOST
        trimmed = requested[:-3] if requested.endswith('/v1') else requested
        canonical = f'{trimmed}/v1'
        return ResolvedLocalBaseURL(
            requested_url=requested,
            canonical_base_url=canonical,
            candidates=(trimmed, canonical),
            models_payload={'data': [{'id': 'qwen3-8b'}, {'id': 'smollm3-3b'}]},
        )

    monkeypatch.setattr('findmyjob.filefirst.advanced_models.probe_lmstudio_base_url', fake_probe)

    profiles = split_model_defaults()

    assert profiles['lmstudio-draft-writer']['base_url'] == f'{LMSTUDIO_DEFAULT_HOST}/v1'
    assert profiles['lmstudio-draft-writer']['model'] == 'qwen3-8b'
    assert profiles['lmstudio-screen-classifier']['model'] == 'smollm3-3b'


def test_install_recommended_split_profiles_uses_lmstudio_defaults(monkeypatch, tmp_path) -> None:
    _split_model_defaults_cached.cache_clear()
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    def fake_probe(base_url, *, timeout=15.0, api_key=None):
        _ = (timeout, api_key)
        requested = str(base_url or '').strip() or LMSTUDIO_DEFAULT_HOST
        trimmed = requested[:-3] if requested.endswith('/v1') else requested
        canonical = f'{trimmed}/v1'
        return ResolvedLocalBaseURL(
            requested_url=requested,
            canonical_base_url=canonical,
            candidates=(trimmed, canonical),
            models_payload={'data': [{'id': LMSTUDIO_DEFAULT_WRITER_MODEL}, {'id': LMSTUDIO_DEFAULT_SCREENING_MODEL}]},
        )

    monkeypatch.setattr('findmyjob.filefirst.advanced_models.probe_lmstudio_base_url', fake_probe)

    result = install_recommended_split_profiles(ws)

    assert result['saved'] is True
    installed = {profile['name']: profile for profile in result['installed']}
    assert set(installed) == {
        'lmstudio-screen-text-router',
        'lmstudio-screen-classifier',
        'lmstudio-screen-extractor',
        'lmstudio-draft-writer',
        'lmstudio-draft-resume-writer',
        'lmstudio-draft-cover-letter-writer',
        'lmstudio-draft-question-answerer',
    }
    assert all(profile['provider'] == 'lmstudio' for profile in installed.values())
    assert all(profile['transport'] == 'local_http' for profile in installed.values())
    assert all(profile['api_key_env'] is None for profile in installed.values())
    assert all(profile['base_url'] == f'{LMSTUDIO_DEFAULT_HOST}/v1' for profile in installed.values())
    assert installed['lmstudio-screen-classifier']['model'] == LMSTUDIO_DEFAULT_SCREENING_MODEL
    assert installed['lmstudio-screen-extractor']['model'] == LMSTUDIO_DEFAULT_SCREENING_MODEL
    assert installed['lmstudio-draft-writer']['model'] == LMSTUDIO_DEFAULT_WRITER_MODEL

    config = AppConfig.load(tmp_path)
    assert config.models['lmstudio-screen-classifier'].api_key_env is None
    assert config.models['lmstudio-draft-question-answerer'].base_url == f'{LMSTUDIO_DEFAULT_HOST}/v1'
