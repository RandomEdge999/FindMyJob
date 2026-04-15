import os
from pathlib import Path

from tomlkit import parse

from findmyjob.core.config import AppConfig, inspect_app_config, write_default_workspace_config
from findmyjob.core.enums import CaptureMode, LogRedactionMode, SubmitEvidenceMode
from findmyjob.core.lmstudio import (
    LMSTUDIO_AUTO_MODEL,
    LMSTUDIO_DEFAULT_HOST,
)
from findmyjob.core.paths import workspace_config_file


def test_default_config_round_trip(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    config = AppConfig.load(tmp_path)
    assert config.policy.default_application_mode.value == "dry_run"
    assert config.policy.require_human_review_for_submit is True
    assert "greenhouse" in config.sources
    assert config.sources["greenhouse"].enabled is True
    assert config.sources["greenhouse"].submit_enabled is False
    assert config.sources["greenhouse"].browser_attach_enabled is False
    assert config.sources["greenhouse"].browser_jobs_url == "https://my.greenhouse.io/jobs"
    assert config.sources["greenhouse"].browser_cdp_url == "http://127.0.0.1:9222"
    assert config.sources["greenhouse"].use_builtin_board_universe is True
    assert config.sources["lever"].enabled is False
    assert config.sources["lever"].submit_enabled is False
    assert config.sources["lever"].use_builtin_board_universe is True
    assert config.sources["ashby"].enabled is False
    assert config.sources["ashby"].submit_enabled is False
    assert config.sources["ashby"].use_builtin_board_universe is True
    assert "lmstudio-writer" in config.models
    assert "lmstudio-question-answerer" in config.models
    assert "lmstudio-extractor" in config.models
    assert "lmstudio-classifier" in config.models
    assert "lmstudio-page-vlm" in config.models
    assert "lmstudio-page-vlm-verifier" in config.models
    lmstudio_writer = config.models["lmstudio-writer"]
    assert lmstudio_writer.supports_structured_output is True
    assert lmstudio_writer.model == LMSTUDIO_AUTO_MODEL
    assert config.models["lmstudio-classifier"].model == LMSTUDIO_AUTO_MODEL
    assert config.models["lmstudio-classifier"].base_url == LMSTUDIO_DEFAULT_HOST
    assert config.search.remote_only is False
    assert config.search.countries == []
    assert config.search.experience_levels == []
    assert config.search.posted_within_days is None
    assert config.privacy.artifact_retention_days == 30
    assert config.privacy.traces == CaptureMode.FAILURES_ONLY
    assert config.privacy.submit_evidence == SubmitEvidenceMode.SUMMARY
    assert config.privacy.log_redaction == LogRedactionMode.SAFE
    assert config.personal.enabled is True
    assert config.personal.source_dir == 'my_personal_information'
    assert config.personal.resume_renderer == 'chatgpt_download'
    assert config.chatgpt_drafting.enabled is True
    assert config.chatgpt_drafting.gpt_url == 'https://chatgpt.com/g/your-custom-resume-cover-letter-writer'
    assert config.chatgpt_drafting.browser_cdp_url == 'http://127.0.0.1:9333'
    assert config.personal.profile_facts_file == '.fmj/local_profile/profile_facts.yaml'
    assert config.personal.saved_search_presets == []
    assert config.personal.enabled_saved_search_presets == []
    assert config.personal.countries == []
    assert config.personal.remote_only is None
    assert config.personal.default_result_limit is None
    assert config.personal.auto_prepare_after_discovery is None
    assert config.autonomous.enabled is True
    assert config.autonomous.browser_mode == "headed"
    assert config.autonomous.max_open_tabs == 6


def test_default_config_without_local_models(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path, include_local_models=False)
    config = AppConfig.load(tmp_path)
    assert config.models == {}


def test_inspect_app_config_blocks_invalid_remote_model_profile(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding="utf-8"))
    doc["models"]["remote-writer"] = {
        "name": "remote-writer",
        "role": "writer",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "local": False,
    }
    config_path.write_text(doc.as_string(), encoding="utf-8")

    config, report = inspect_app_config(tmp_path)

    assert config is not None
    assert report.blocked_count >= 2
    assert {finding.key for finding in report.findings} >= {"models.remote-writer.base_url", "models.remote-writer.api_key_env"}


def test_inspect_app_config_parses_privacy_settings(tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding="utf-8"))
    privacy = doc["privacy"]
    privacy["artifact_retention_days"] = 14
    privacy["traces"] = "all"
    privacy["dom_snapshots"] = "off"
    privacy["screenshots"] = "failures_only"
    privacy["submit_evidence"] = "full"
    privacy["log_redaction"] = "strict"
    config_path.write_text(doc.as_string(), encoding="utf-8")

    config = AppConfig.load(tmp_path)

    assert config.privacy.artifact_retention_days == 14
    assert config.privacy.traces == CaptureMode.ALL
    assert config.privacy.dom_snapshots == CaptureMode.OFF
    assert config.privacy.screenshots == CaptureMode.FAILURES_ONLY
    assert config.privacy.submit_evidence == SubmitEvidenceMode.FULL
    assert config.privacy.log_redaction == LogRedactionMode.STRICT


def test_app_config_load_populates_workspace_dotenv(monkeypatch, tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    (tmp_path / '.env').write_text('FMJ_TEST_ENV=workspace-secret\n', encoding='utf-8')
    monkeypatch.delenv('FMJ_TEST_ENV', raising=False)

    AppConfig.load(tmp_path)

    assert os.environ['FMJ_TEST_ENV'] == 'workspace-secret'


def test_app_config_load_keeps_process_env_over_workspace_dotenv(monkeypatch, tmp_path: Path) -> None:
    config_path = workspace_config_file(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_default_workspace_config(config_path)
    (tmp_path / '.env').write_text('FMJ_TEST_ENV=workspace-secret\n', encoding='utf-8')
    monkeypatch.setenv('FMJ_TEST_ENV', 'process-secret')

    AppConfig.load(tmp_path)

    assert os.environ['FMJ_TEST_ENV'] == 'process-secret'

