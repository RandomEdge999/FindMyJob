from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from findmyjob.core.lmstudio import (
    LMSTUDIO_PROVIDER,
    canonical_lmstudio_base_url,
    lmstudio_available_model_ids,
    lmstudio_model_entries,
    lmstudio_model_name_matches,
    normalize_lmstudio_model_name,
    resolve_lmstudio_model_id_or_default,
)
from findmyjob.filefirst.models import LocalModelSettings

log = logging.getLogger("findmyjob.llm")


class LLMUnavailableError(Exception):
    """Raised when the local LLM server is unreachable after retries."""


class LocalGemmaClient:
    def __init__(self, settings: LocalModelSettings, max_retries: int = 3, backoff_base: float = 2.0) -> None:
        self.settings = settings
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def _is_long_running_writer_model(self) -> bool:
        model_name = str(self.settings.model or "").strip().casefold()
        return "qwen" in model_name

    def list_models_payload(self) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(f"{self._base_url().rstrip('/')}/models")
            response.raise_for_status()
            return response.json()

    def list_models(self) -> list[str]:
        payload = self.list_models_payload()
        return lmstudio_available_model_ids(payload)

    def server_props(self) -> dict[str, Any] | None:
        with httpx.Client(timeout=20.0) as client:
            try:
                response = client.get(f"{self._server_root_url()}/props")
                response.raise_for_status()
            except httpx.HTTPError:
                return None
            return response.json()

    def verify(self) -> dict[str, Any]:
        models_payload = self.list_models_payload()
        available = lmstudio_available_model_ids(models_payload)
        matching_model = self._model_metadata(models_payload)
        props = self.server_props()
        server_context_window = self._server_context_window(props)
        train_context_window = self._train_context_window(matching_model)
        preferred_context_window = self.settings.preferred_context_window
        text = self.generate_text(
            "You are a health-check assistant. Reply with the single word ready.",
            "Reply with the single word ready.",
        )
        payload = self.generate_json(
            "You are a health-check assistant. Return only JSON.",
            'Return {"ok": true, "model": "' + self.settings.model + '"}',
        )
        return {
            "base_url": self.settings.base_url,
            "model": self.settings.model,
            "available_models": available,
            "model_present": matching_model is not None,
            "text_ok": text.strip().lower().startswith("ready"),
            "json_ok": bool(payload.get("ok")),
            "train_context_window": train_context_window,
            "server_context_window": server_context_window,
            "preferred_context_window": preferred_context_window,
            "context_ok": server_context_window is None or server_context_window >= preferred_context_window,
            "reasoning_format": self._reasoning_format(props),
            "modalities": props.get("modalities", {}) if isinstance(props, dict) else {},
        }

    def generate_text(self, system_prompt: str, prompt: str) -> str:
        response = self._chat(system_prompt, prompt, expect_json=False)
        return self._strip_thinking_tags(response["choices"][0]["message"]["content"])

    def generate_json(self, system_prompt: str, prompt: str) -> dict[str, Any]:
        response = self._chat(system_prompt, prompt, expect_json=True)
        content = self._strip_thinking_tags(response["choices"][0]["message"]["content"])
        try:
            payload = self._parse_json_content(content)
        except RuntimeError as exc:
            repaired = self._repair_json_content(content, str(exc))
            payload = self._parse_json_content(repaired)
        if not isinstance(payload, dict):
            raise RuntimeError("Local model returned non-object JSON content.")
        return payload

    def health_check(self) -> dict[str, Any]:
        """Quick health check to verify the LLM server is responsive.

        Returns a dict with 'healthy' bool and diagnostic info.
        """
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{self._base_url().rstrip('/')}/models")
                response.raise_for_status()
                elapsed = time.monotonic() - t0
                payload = response.json()
                model_ids = lmstudio_available_model_ids(payload)
                model_present = resolve_lmstudio_model_id_or_default(self.settings.model, payload) is not None
                return {
                    "healthy": True,
                    "model_present": model_present,
                    "available_models": model_ids,
                    "response_time_ms": round(elapsed * 1000),
                }
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            elapsed = time.monotonic() - t0
            return {
                "healthy": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "response_time_ms": round(elapsed * 1000),
            }

    def _chat(self, system_prompt: str, prompt: str, *, expect_json: bool) -> dict[str, Any]:
        resolved_model = self.settings.model
        try:
            catalog = self.list_models_payload()
            resolved_model = resolve_lmstudio_model_id_or_default(self.settings.model, catalog) or self.settings.model
        except Exception:
            resolved_model = self.settings.model
        payload = {
            "model": resolved_model,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}
        return self._chat_with_retry(payload, expect_json=expect_json)

    def _chat_with_retry(self, payload: dict[str, Any], *, expect_json: bool) -> dict[str, Any]:
        last_error: Exception | None = None
        effective_max_retries = 1 if self._is_long_running_writer_model() else self.max_retries
        for attempt in range(1, effective_max_retries + 1):
            try:
                return self._do_chat_request(payload, expect_json=expect_json)
            except httpx.ConnectError as exc:
                last_error = exc
                log.warning("[llm] Connection failed (attempt %d/%d): %s", attempt, effective_max_retries, exc)
                if attempt < effective_max_retries:
                    wait = self.backoff_base ** attempt
                    log.info("[llm] Retrying in %.1fs...", wait)
                    time.sleep(wait)
            except httpx.TimeoutException as exc:
                last_error = exc
                log.warning("[llm] Timeout (attempt %d/%d): %s", attempt, effective_max_retries, exc)
                if attempt < effective_max_retries:
                    wait = self.backoff_base ** attempt
                    log.info("[llm] Retrying in %.1fs...", wait)
                    time.sleep(wait)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code
                if status_code in {429, 500, 502, 503, 504}:
                    log.warning("[llm] HTTP %d (attempt %d/%d): %s", status_code, attempt, effective_max_retries, exc)
                    if attempt < effective_max_retries:
                        wait = self.backoff_base ** attempt
                        log.info("[llm] Retrying in %.1fs...", wait)
                        time.sleep(wait)
                else:
                    raise
        raise LLMUnavailableError(
            f"Local model server at {self._base_url()} is unavailable after {effective_max_retries} attempts: {last_error}"
        ) from last_error

    def _do_chat_request(self, payload: dict[str, Any], *, expect_json: bool) -> dict[str, Any]:
        read_timeout = 900.0 if self._is_long_running_writer_model() else 180.0
        with httpx.Client(timeout=httpx.Timeout(connect=15.0, read=read_timeout, write=30.0, pool=30.0)) as client:
            response = client.post(f"{self._base_url().rstrip('/')}/chat/completions", json=payload)
            if expect_json and response.status_code >= 400:
                retry_payload = dict(payload)
                retry_payload.pop("response_format", None)
                response = client.post(f"{self._base_url().rstrip('/')}/chat/completions", json=retry_payload)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _strip_thinking_tags(content: Any) -> str:
        text = str(content or "")
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
        return text.strip()

    def _base_url(self) -> str:
        base_url = str(self.settings.base_url or "").strip()
        if str(self.settings.provider or "").strip().lower() == LMSTUDIO_PROVIDER:
            return canonical_lmstudio_base_url(base_url)
        return base_url

    def _parse_json_content(self, content: Any) -> Any:
        if isinstance(content, (dict, list)):
            return content
        text = str(content or "").strip()
        if not text:
            raise RuntimeError("Model returned an empty JSON response.")
        candidates: list[str] = [text]
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if fence_match:
            candidates.append(fence_match.group(1).strip())
        object_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if object_match:
            candidates.append(object_match.group(1).strip())
        decoder = json.JSONDecoder()
        seen: set[str] = set()
        last_error: json.JSONDecodeError | None = None
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_error = exc
            for start_char in ("{", "["):
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
        detail = f"{type(last_error).__name__}: {last_error}" if last_error is not None else "no JSON object found"
        raise RuntimeError(f"invalid JSON response: {detail}")

    def _repair_json_content(self, content: Any, detail: str) -> Any:
        text = str(content or "").strip()
        if not text:
            raise RuntimeError("Model returned an empty JSON response.")
        repair_prompt = (
            "The following model output was intended to be a JSON object but failed to parse. "
            "Repair it into a valid JSON object without changing the intended keys or values. "
            "Escape embedded newlines and quotes when needed. Return only the repaired JSON object.\n\n"
            f"Parse error: {detail}\n\n"
            "Malformed output:\n"
            f"{text}"
        )
        response = self._chat(
            "You repair malformed JSON. Return only a valid JSON object with no markdown fences or commentary.",
            repair_prompt,
            expect_json=True,
        )
        return response["choices"][0]["message"]["content"]

    def _server_root_url(self) -> str:
        base_url = self.settings.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return base_url[:-3]
        return base_url

    def _model_metadata(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        resolved_model_id = resolve_lmstudio_model_id_or_default(self.settings.model, payload)
        if resolved_model_id is None:
            return None
        for item in lmstudio_model_entries(payload):
            if str(item.get("id") or "").strip() == resolved_model_id:
                return item
        return None

    def _model_name_matches(self, requested: str, candidate: str) -> bool:
        return lmstudio_model_name_matches(requested, candidate)

    def _normalize_model_name(self, value: str) -> str:
        return normalize_lmstudio_model_name(value)

    def _server_context_window(self, props: dict[str, Any] | None) -> int | None:
        if not isinstance(props, dict):
            return None
        generation_settings = props.get("default_generation_settings")
        if not isinstance(generation_settings, dict):
            return None
        value = generation_settings.get("n_ctx")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _train_context_window(self, metadata: dict[str, Any] | None) -> int | None:
        if not isinstance(metadata, dict):
            return None
        meta = metadata.get("meta")
        if not isinstance(meta, dict):
            return None
        value = meta.get("n_ctx_train")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _reasoning_format(self, props: dict[str, Any] | None) -> str | None:
        if not isinstance(props, dict):
            return None
        generation_settings = props.get("default_generation_settings")
        if not isinstance(generation_settings, dict):
            return None
        params = generation_settings.get("params")
        if not isinstance(params, dict):
            return None
        value = str(params.get("reasoning_format") or "").strip()
        return value or None


