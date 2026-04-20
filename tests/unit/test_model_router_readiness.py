import anyio
import pytest

from findmyjob.core.config import AppConfig
from findmyjob.core.enums import ModelRole
from findmyjob.core.types import ModelProfile
from findmyjob.model_router.router import ModelRouter


def test_model_router_inspection_surfaces_missing_roles_secret_and_fallbacks() -> None:
    config = AppConfig(
        models={
            "writer": ModelProfile(
                name="writer",
                role=ModelRole.WRITER,
                provider="openai",
                model="gpt-4o-mini",
                local=False,
                base_url="https://api.example.com/v1",
                api_key_env="OPENAI_API_KEY",
                fallback_chain=["missing-backup"],
                policy_tags=["operator"],
            )
        }
    )
    router = ModelRouter(config)

    inspection = router.inspect_profiles()
    profile = inspection["profiles"][0]

    assert set(inspection["missing_required_roles"]) == {"question_answerer", "classifier"}
    assert profile["status"] == "blocked"
    assert profile["transport"] == "remote_http"
    assert profile["secret_satisfied"] is False
    assert "missing fallbacks: missing-backup" in profile["issues"]
    assert "secret `OPENAI_API_KEY` not found in env or keyring" in profile["issues"]


def test_model_router_test_profile_returns_misconfigured_for_missing_secret() -> None:
    profile = ModelProfile(
        name="writer",
        role=ModelRole.WRITER,
        provider="openai",
        model="gpt-4o-mini",
        local=False,
        base_url="https://api.example.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    router = ModelRouter(AppConfig(models={"writer": profile}))

    result = __import__("anyio").run(router.test_profile, profile)

    assert result["ok"] is False
    assert result["classification"] == "misconfigured"
    assert any("OPENAI_API_KEY" in issue for issue in result["issues"])


def test_launch_profile_passes_with_required_roles_bound() -> None:
    config = AppConfig(
        models={
            "writer": ModelProfile(
                name="writer",
                role=ModelRole.WRITER,
                provider="lmstudio",
                model="qwen3-8b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
            "classifier": ModelProfile(
                name="classifier",
                role=ModelRole.CLASSIFIER,
                provider="lmstudio",
                model="smollm3-3b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
            "qa": ModelProfile(
                name="qa",
                role=ModelRole.QUESTION_ANSWERER,
                provider="lmstudio",
                model="qwen3-8b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
        }
    )
    router = ModelRouter(config)

    report = router.inspect_launch_profile()

    assert report.overall_status == "pass"
    assert report.missing_required_roles == []
    assert report.warning_count == 0
    assert report.fail_count == 0
    assert report.summary == "3/3 required launch roles ready"


def test_launch_profile_fails_when_required_role_is_missing() -> None:
    config = AppConfig(
        models={
            "writer": ModelProfile(
                name="writer",
                role=ModelRole.WRITER,
                provider="lmstudio",
                model="qwen3-8b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
        }
    )
    router = ModelRouter(config)

    report = router.inspect_launch_profile()

    assert report.overall_status == "fail"
    assert set(report.missing_required_roles) == {"classifier", "question_answerer"}
    assert report.fail_count == 2
    assert any(role.role == "classifier" and role.status == "fail" for role in report.roles)
    assert any(role.role == "question_answerer" and role.status == "fail" for role in report.roles)


def test_launch_profile_fails_when_required_role_uses_remote_transport() -> None:
    config = AppConfig(
        models={
            "writer": ModelProfile(
                name="writer",
                role=ModelRole.WRITER,
                provider="lmstudio",
                model="qwen3-8b",
                transport="remote_http",
                local=False,
                base_url="https://example.invalid/v1",
            ),
            "classifier": ModelProfile(
                name="classifier",
                role=ModelRole.CLASSIFIER,
                provider="lmstudio",
                model="smollm3-3b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
            "qa": ModelProfile(
                name="qa",
                role=ModelRole.QUESTION_ANSWERER,
                provider="lmstudio",
                model="qwen3-8b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
        }
    )
    router = ModelRouter(config)

    report = router.inspect_launch_profile()

    assert report.overall_status == "fail"
    writer = next(role for role in report.roles if role.role == "writer")
    assert writer.status == "fail"
    assert "launch contract requires LM Studio local_http transport" in writer.issues


def test_launch_profile_accepts_openrouter_for_screening_and_qa_roles() -> None:
    # Phase 4/6: screening + question-answering roles may run on OpenRouter (remote_http).
    config = AppConfig(
        models={
            "writer": ModelProfile(
                name="writer",
                role=ModelRole.WRITER,
                provider="lmstudio",
                model="qwen3-8b",
                transport="local_http",
                local=True,
                base_url="http://127.0.0.1:1234/v1",
            ),
            "classifier": ModelProfile(
                name="classifier",
                role=ModelRole.CLASSIFIER,
                provider="openrouter",
                model="meta-llama/llama-3.1-8b-instruct",
                transport="remote_http",
                local=False,
                base_url="https://openrouter.ai/api/v1",
                api_key_env="OPENROUTER_API_KEY",
            ),
            "qa": ModelProfile(
                name="qa",
                role=ModelRole.QUESTION_ANSWERER,
                provider="openrouter",
                model="meta-llama/llama-3.1-8b-instruct",
                transport="remote_http",
                local=False,
                base_url="https://openrouter.ai/api/v1",
                api_key_env="OPENROUTER_API_KEY",
            ),
        }
    )
    router = ModelRouter(config)

    report = router.inspect_launch_profile()

    classifier = next(role for role in report.roles if role.role == "classifier")
    qa = next(role for role in report.roles if role.role == "question_answerer")
    # Should not be 'fail' just for running remote; readiness may still warn about missing secret.
    assert classifier.status != "fail"
    assert qa.status != "fail"
    # And no launch-contract hard-fail risk should fire on these roles.
    assert not any("classifier profile" in risk and "not using" in risk for risk in report.risks)
    assert not any("question_answerer profile" in risk and "not using" in risk for risk in report.risks)


def test_generate_text_surfaces_provider_error_payload(monkeypatch) -> None:
    profile = ModelProfile(
        name="lmstudio-draft-cover-letter-writer",
        role=ModelRole.WRITER,
        provider="lmstudio",
        model="lmstudio-community/Qwen3-8B-GGUF",
        local=True,
        transport="local_http",
        base_url="http://127.0.0.1:1234",
    )
    router = ModelRouter(AppConfig(models={"writer": profile}))

    async def fake_chat_completion(self, profile, payload, *, mode):
        _ = (self, profile, payload, mode)
        return {
            "error": {
                "message": "You exceeded your current quota, please check your plan and billing details.",
                "code": "insufficient_quota",
                "type": "invalid_request_error",
            }
        }

    monkeypatch.setattr(ModelRouter, "_chat_completion", fake_chat_completion)

    with pytest.raises(RuntimeError, match="quota"):
        anyio.run(router.generate_text, ModelRole.WRITER, "Return one word.")


def test_extract_chat_content_accepts_structured_message_content() -> None:
    router = ModelRouter(AppConfig())
    content = router._extract_chat_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "first "},
                            {"type": "text", "text": "second"},
                        ]
                    }
                }
            ]
        },
        profile_name="writer",
    )
    assert content == "first second"


def test_base_payload_disables_screening_thinking_and_caps_writer_completion_tokens() -> None:
    router = ModelRouter(AppConfig())
    classifier = ModelProfile(
        name="classifier",
        role=ModelRole.CLASSIFIER,
        provider="lmstudio",
        model="gemma-4-e4b-it",
        transport="local_http",
        local=True,
        base_url="http://127.0.0.1:1234/v1",
        temperature=0.0,
        max_tokens=4096,
    )
    writer = ModelProfile(
        name="writer",
        role=ModelRole.WRITER,
        provider="lmstudio",
        model="qwen3.5-35b-a3b-uncensored-hauhaucs-aggressive",
        transport="local_http",
        local=True,
        base_url="http://127.0.0.1:1234/v1",
        temperature=0.7,
        max_tokens=32000,
    )

    classifier_payload = router._base_payload(classifier, "prompt", "system", role=ModelRole.CLASSIFIER)
    writer_payload = router._base_payload(writer, "prompt", "system", role=ModelRole.WRITER)

    assert classifier_payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert writer_payload["max_completion_tokens"] == 16384


def test_base_payload_disables_thinking_when_no_think_marker_is_present() -> None:
    router = ModelRouter(AppConfig())
    writer = ModelProfile(
        name="writer",
        role=ModelRole.WRITER,
        provider="lmstudio",
        model="qwen3.5-35b-a3b-uncensored-hauhaucs-aggressive",
        transport="local_http",
        local=True,
        base_url="http://127.0.0.1:1234/v1",
        temperature=0.7,
        max_tokens=8192,
    )

    payload = router._base_payload(writer, "/no-think\nRewrite this resume.", "system", role=ModelRole.WRITER)

    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_fetch_local_model_catalog_dedupes_concurrent_requests(monkeypatch) -> None:
    router = ModelRouter(AppConfig())
    calls: list[str] = []
    payload = {'data': [{'id': 'model-1'}]}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, str]]]:
            return payload

    async def fake_get(self, url):
        _ = self
        calls.append(url)
        await anyio.sleep(0.05)
        return _Response()

    monkeypatch.setattr('httpx.AsyncClient.get', fake_get)

    async def run() -> list[dict[str, object] | None]:
        results: list[dict[str, object] | None] = []

        async def fetch() -> None:
            results.append(await router._fetch_local_model_catalog('http://127.0.0.1:1234/v1'))

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(fetch)
            task_group.start_soon(fetch)
        return results

    results = anyio.run(run)

    assert calls == ['http://127.0.0.1:1234/v1/models']
    assert results == [payload, payload]


def test_discover_remote_http_models_returns_ids_from_openrouter_style_payload(monkeypatch) -> None:
    router = ModelRouter(AppConfig())
    captured_headers: dict[str, str] = {}
    captured_url: dict[str, str] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {
                'data': [
                    {'id': 'meta-llama/llama-3.1-8b-instruct'},
                    {'id': 'anthropic/claude-3.5-sonnet'},
                    {'id': 'meta-llama/llama-3.1-8b-instruct'},  # duplicate
                ]
            }

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured_headers.update(kwargs.get('headers') or {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            captured_url['url'] = url
            return _Response()

    monkeypatch.setattr('findmyjob.model_router.router.httpx.AsyncClient', _FakeClient)

    ids = anyio.run(
        lambda: router.discover_remote_http_models(
            base_url='https://openrouter.ai/api/v1',
            api_key='sk-test',
        )
    )

    assert ids == ['meta-llama/llama-3.1-8b-instruct', 'anthropic/claude-3.5-sonnet']
    assert captured_url['url'] == 'https://openrouter.ai/api/v1/models'
    assert captured_headers.get('Authorization') == 'Bearer sk-test'
