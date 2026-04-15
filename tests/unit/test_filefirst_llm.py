from __future__ import annotations

from findmyjob.filefirst.llm import LocalGemmaClient
from findmyjob.filefirst.models import LocalModelSettings


def test_local_gemma_verify_reports_context_windows(monkeypatch) -> None:
    monkeypatch.setattr(
        LocalGemmaClient,
        "list_models_payload",
        lambda self: {
            "data": [
                {
                    "id": "gemma-4-E4B-it",
                    "aliases": ["gemma-4-E4B-it"],
                    "meta": {"n_ctx_train": 131072},
                }
            ]
        },
    )
    monkeypatch.setattr(
        LocalGemmaClient,
        "server_props",
        lambda self: {
            "default_generation_settings": {
                "n_ctx": 65536,
                "params": {"reasoning_format": "none"},
            },
            "modalities": {"vision": False, "audio": False},
        },
    )
    monkeypatch.setattr(LocalGemmaClient, "generate_text", lambda self, system_prompt, prompt: "ready")
    monkeypatch.setattr(LocalGemmaClient, "generate_json", lambda self, system_prompt, prompt: {"ok": True})

    result = LocalGemmaClient(LocalModelSettings(model="gemma-4-E4B-it")).verify()

    assert result["model_present"] is True
    assert result["train_context_window"] == 131072
    assert result["server_context_window"] == 65536
    assert result["preferred_context_window"] == 131072
    assert result["context_ok"] is False
    assert result["reasoning_format"] == "none"


def test_local_gemma_verify_flags_small_runtime_context(monkeypatch) -> None:
    monkeypatch.setattr(
        LocalGemmaClient,
        "list_models_payload",
        lambda self: {
            "data": [
                {
                    "id": "gemma-4-E4B-it",
                    "aliases": ["gemma-4-E4B-it"],
                    "meta": {"n_ctx_train": 131072},
                }
            ]
        },
    )
    monkeypatch.setattr(
        LocalGemmaClient,
        "server_props",
        lambda self: {
            "default_generation_settings": {
                "n_ctx": 8192,
                "params": {"reasoning_format": "none"},
            }
        },
    )
    monkeypatch.setattr(LocalGemmaClient, "generate_text", lambda self, system_prompt, prompt: "ready")
    monkeypatch.setattr(LocalGemmaClient, "generate_json", lambda self, system_prompt, prompt: {"ok": True})

    result = LocalGemmaClient(LocalModelSettings(preferred_context_window=65536)).verify()

    assert result["server_context_window"] == 8192
    assert result["preferred_context_window"] == 65536
    assert result["context_ok"] is False


def test_local_gemma_generate_json_repairs_invalid_response(monkeypatch) -> None:
    calls: list[tuple[str, str, bool]] = []

    def fake_chat(self, system_prompt: str, prompt: str, *, expect_json: bool):
        calls.append((system_prompt, prompt, expect_json))
        if len(calls) == 1:
            return {
                'choices': [
                    {
                        'message': {
                            'content': '```json\n{"ok": true, "report_markdown": "line 1\nline 2"}\n```'
                        }
                    }
                ]
            }
        return {
            'choices': [
                {
                    'message': {
                        'content': '{"ok": true, "report_markdown": "line 1\\nline 2"}'
                    }
                }
            ]
        }

    monkeypatch.setattr(LocalGemmaClient, '_chat', fake_chat)

    payload = LocalGemmaClient(LocalModelSettings()).generate_json('Return JSON.', 'Return a JSON object.')

    assert payload['ok'] is True
    assert payload['report_markdown'] == 'line 1\nline 2'
    assert len(calls) == 2
    assert calls[1][2] is True
