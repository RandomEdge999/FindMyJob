from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.config import AppConfig
from findmyjob.core.enums import CompanySizeBucket, FactKind, Sensitivity
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery, ProfileFact, SavedSearch
from findmyjob.db.repositories import JobRepository, ProfileRepository, SavedSearchRepository
from findmyjob.sources.normalizer import build_normalized_job

runner = CliRunner()


def _seed_workspace(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    with runtime.session_scope() as session:
        SavedSearchRepository(session).save(SavedSearch(name='backend-core', query_payload=JobSearchQuery(title_keywords=['backend'], limit=50)))
        repo = ProfileRepository(session)
        repo.upsert_fact(ProfileFact(fact_id='onboard.personal.skill.python', kind=FactKind.SKILL, payload={'name': 'Python', 'summary': 'Python'}, sensitivity=Sensitivity.LOW, provenance='onboarding:mixed'))
        repo.upsert_fact(ProfileFact(fact_id='onboard.personal.personal.demographic', kind=FactKind.PERSONAL, payload={'category': 'demographic', 'value': 'Asian'}, sensitivity=Sensitivity.HIGH, allowed_for_generation=False, disallowed=True, provenance='onboarding:txt'))




def _seed_job(tmp_path: Path) -> str:
    runtime = AppRuntime.bootstrap(tmp_path)
    posting = build_normalized_job(
        company_name='Acme',
        title='Backend CLI Engineer',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id='cli-1',
        posting_url='https://boards.greenhouse.io/acme/jobs/cli-1',
        apply_url='https://boards.greenhouse.io/acme/jobs/cli-1',
        location_raw='Remote - United States',
        employment_type='full_time',
        compensation=None,
        description='Backend engineering role.',
        company_size_bucket=CompanySizeBucket.MIDSIZE,
    )
    with runtime.session_scope() as session:
        return JobRepository(session).upsert_job(posting).id

def test_personal_prefs_cli_set_show_reset(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            'personal', 'prefs', 'set',
            '--workspace', str(tmp_path),
            '--enabled-preset', 'backend-core',
            '--country', 'US',
            '--remote-only',
            '--workplace-type', 'remote',
            '--experience-level', 'entry_level',
            '--result-limit', '25',
            '--auto-prepare',
        ],
    )
    assert result.exit_code == 0, result.output
    assert 'Updated personal preferences.' in result.output

    config = AppConfig.load(tmp_path)
    assert config.personal.enabled_saved_search_presets == ['backend-core']
    assert config.personal.countries == ['US']
    assert config.personal.remote_only is True
    assert config.personal.default_result_limit == 25
    assert config.personal.auto_prepare_after_discovery is True

    shown = runner.invoke(app, ['personal', 'prefs', 'show', '--workspace', str(tmp_path)])
    assert shown.exit_code == 0, shown.output
    assert 'Enabled Presets' in shown.output
    assert 'backend-core' in shown.output

    reset = runner.invoke(app, ['personal', 'prefs', 'reset', '--workspace', str(tmp_path)])
    assert reset.exit_code == 0, reset.output
    reloaded = AppConfig.load(tmp_path)
    assert reloaded.personal.enabled_saved_search_presets == []
    assert reloaded.personal.remote_only is None
    assert reloaded.personal.default_result_limit is None


def test_personal_facts_cli_allow_disallow_inspect_delete(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    runtime = AppRuntime.bootstrap(tmp_path)

    listed = runner.invoke(app, ['personal', 'facts', 'list', '--workspace', str(tmp_path), '--onboarding-only'])
    assert listed.exit_code == 0, listed.output
    assert 'onboard.personal.skill.python' in listed.output
    assert 'onboard.personal.personal.demographic' in listed.output

    inspected = runner.invoke(app, ['personal', 'facts', 'inspect', 'onboard.personal.personal.demographic', '--workspace', str(tmp_path)])
    assert inspected.exit_code == 0, inspected.output
    assert 'demographic' in inspected.output

    disallowed = runner.invoke(app, ['personal', 'facts', 'disallow', 'onboard.personal.skill.python', '--workspace', str(tmp_path)])
    assert disallowed.exit_code == 0, disallowed.output
    with runtime.session_scope() as session:
        fact = next(item for item in ProfileRepository(session).list_facts() if item.fact_id == 'onboard.personal.skill.python')
        assert fact.allowed_for_generation is False
        assert fact.disallowed is True

    allowed = runner.invoke(app, ['personal', 'facts', 'allow', 'onboard.personal.skill.python', '--workspace', str(tmp_path)])
    assert allowed.exit_code == 0, allowed.output
    with runtime.session_scope() as session:
        fact = next(item for item in ProfileRepository(session).list_facts() if item.fact_id == 'onboard.personal.skill.python')
        assert fact.allowed_for_generation is True
        assert fact.disallowed is False

    deleted = runner.invoke(app, ['personal', 'facts', 'delete', 'onboard.personal.skill.python', '--workspace', str(tmp_path), '--yes'])
    assert deleted.exit_code == 0, deleted.output
    with runtime.session_scope() as session:
        fact_ids = [item.fact_id for item in ProfileRepository(session).list_facts()]
        assert 'onboard.personal.skill.python' not in fact_ids





def test_personal_triage_cli_commands(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    job_id = _seed_job(tmp_path)

    shortlisted = runner.invoke(app, ['personal', 'shortlist', job_id, '--workspace', str(tmp_path), '--reason', 'top_pick'])
    assert shortlisted.exit_code == 0, shortlisted.output
    assert 'Shortlisted job' in shortlisted.output

    explained = runner.invoke(app, ['personal', 'explain', job_id, '--workspace', str(tmp_path), '--json'])
    assert explained.exit_code == 0, explained.output
    assert '"status": "shortlisted"' in explained.output

    dismissed = runner.invoke(
        app,
        ['personal', 'dismiss', job_id, '--workspace', str(tmp_path), '--reason', 'noise', '--scope', 'company-title'],
    )
    assert dismissed.exit_code == 0, dismissed.output
    assert 'Created 1 suppression rule' in dismissed.output

    decisions = runner.invoke(app, ['personal', 'decisions', '--workspace', str(tmp_path)])
    assert decisions.exit_code == 0, decisions.output
    assert 'Suppression Rules' in decisions.output
    assert 'company_title' in decisions.output

    unsuppressed = runner.invoke(app, ['personal', 'unsuppress', job_id, '--workspace', str(tmp_path), '--scope', 'all'])
    assert unsuppressed.exit_code == 0, unsuppressed.output
    assert 'Cleared 1 suppression rule' in unsuppressed.output

    explained_again = runner.invoke(app, ['personal', 'explain', job_id, '--workspace', str(tmp_path), '--json'])
    assert explained_again.exit_code == 0, explained_again.output
    assert '"status": "new"' in explained_again.output
    assert '"suppressed": false' in explained_again.output
