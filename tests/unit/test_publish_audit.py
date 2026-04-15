from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "publish_audit.py"
    spec = importlib.util.spec_from_file_location("publish_audit", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_publish_audit_flags_local_override_identity_leak(tmp_path: Path) -> None:
    module = _module()
    override_profile = tmp_path / ".fmj" / "local-overrides" / "filefirst" / "config" / "profile.yml"
    override_profile.parent.mkdir(parents=True, exist_ok=True)
    override_profile.write_text(
        "candidate:\n  name: Real Local User\n  email: real.user@example.net\n  location: Secret City, ST, US\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("Hello from Real Local User\n", encoding="utf-8")

    findings = module.run_audit(tmp_path)

    assert any(finding.kind == "local_identity_leak" for finding in findings)


def test_publish_audit_ignores_placeholder_secret_examples(tmp_path: Path) -> None:
    module = _module()
    (tmp_path / ".env.example").write_text(
        "FMJ_GMAIL_APP_PASSWORD=your_16_char_gmail_app_password\nOPENAI_API_KEY=example-key\n",
        encoding="utf-8",
    )

    findings = module.run_audit(tmp_path)

    assert findings == []
