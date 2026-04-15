from pathlib import Path

from scripts.lmstudio_preflight import run_preflight


class _Router:
    def inspect_launch_profile(self):
        return type('LaunchProfile', (), {'transport_mix': 'all_remote'})()


def test_lmstudio_preflight_fails_remote_only_transport(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr('scripts.lmstudio_preflight.load_model_router', lambda ws: _Router())

    ok, detail = run_preflight(tmp_path)

    assert ok is False
    assert detail == 'Launch contract requires at least one LM Studio local HTTP model profile.'
