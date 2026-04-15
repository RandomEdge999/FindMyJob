from __future__ import annotations

from pathlib import Path

from findmyjob.core.enums import ModelRole
from findmyjob.filefirst.modes import ModeRunner
from findmyjob.filefirst.workspace import FileWorkspace


def test_eval_mode_uses_classifier_then_writer_fallback(monkeypatch, tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    calls: list[ModelRole] = []

    class _Router:
        async def generate_json_with_profile(self, role, prompt, *, system_prompt):
            _ = (prompt, system_prompt)
            calls.append(role)
            if role == ModelRole.CLASSIFIER:
                raise RuntimeError("classifier failed")
            return {"score": 4.5}, "writer-profile"

    monkeypatch.setattr("findmyjob.filefirst.modes.load_model_router", lambda workspace: _Router())

    runner = ModeRunner(ws)
    payload = runner.run_json("eval", {"job": {"title": "Backend Engineer"}})

    assert payload["score"] == 4.5
    assert calls == [ModelRole.CLASSIFIER, ModelRole.WRITER]
    assert runner.last_role == ModelRole.WRITER


def test_eval_mode_retries_when_primary_role_returns_error_payload(monkeypatch, tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    calls: list[ModelRole] = []

    class _Router:
        async def generate_json_with_profile(self, role, prompt, *, system_prompt):
            _ = (prompt, system_prompt)
            calls.append(role)
            if role == ModelRole.CLASSIFIER:
                return {"error": "Invalid JSON: Expecting value"}, "classifier-profile"
            return {"score": 4.2}, "writer-profile"

    monkeypatch.setattr("findmyjob.filefirst.modes.load_model_router", lambda workspace: _Router())

    runner = ModeRunner(ws)
    payload = runner.run_json("eval", {"job": {"title": "Backend Engineer"}})

    assert payload["score"] == 4.2
    assert calls == [ModelRole.CLASSIFIER, ModelRole.CLASSIFIER, ModelRole.CLASSIFIER, ModelRole.WRITER]
    assert runner.last_role == ModelRole.WRITER


def test_eval_mode_retries_same_role_before_fallback(monkeypatch, tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    calls: list[ModelRole] = []
    classifier_attempts = {"count": 0}

    class _Router:
        async def generate_json_with_profile(self, role, prompt, *, system_prompt):
            _ = (prompt, system_prompt)
            calls.append(role)
            if role == ModelRole.CLASSIFIER:
                classifier_attempts["count"] += 1
                if classifier_attempts["count"] < 3:
                    raise RuntimeError("invalid JSON response: JSONDecodeError: Expecting value: line 1 column 1 (char 0)")
                return {"score": 4.3}, "classifier-profile"
            return {"score": 4.1}, "writer-profile"

    monkeypatch.setattr("findmyjob.filefirst.modes.load_model_router", lambda workspace: _Router())

    runner = ModeRunner(ws)
    payload = runner.run_json("eval", {"job": {"title": "Backend Engineer"}})

    assert payload["score"] == 4.3
    assert calls == [ModelRole.CLASSIFIER, ModelRole.CLASSIFIER, ModelRole.CLASSIFIER]
    assert runner.last_role == ModelRole.CLASSIFIER
