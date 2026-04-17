"""Seed allowlist for Vulture false positives in dynamic runtime paths."""

from findmyjob.cli import main as cli_main
from findmyjob.cli.filefirst import register_filefirst_commands
from findmyjob.db import models as db_models


# Typer apps and nested registration are wired up dynamically.
cli_main.app
cli_main.config_app
cli_main.models_app
cli_main.profile_app
cli_main.discover_app
cli_main.prepare_app
cli_main.apply_app
cli_main.jobs_app
cli_main.searches_app
cli_main.review_app
cli_main.onboard_app
cli_main.personal_app
cli_main.personal_prefs_app
cli_main.personal_facts_app
cli_main.personal_preview_app
cli_main.support_app
cli_main.ledger_app
cli_main.runs_app
cli_main.sources_app
cli_main.greenhouse_app
cli_main.auto_app
cli_main.questions_app
cli_main.boards_app
cli_main.db_app
cli_main.workflow_app
register_filefirst_commands


# SQLAlchemy relationships are accessed through ORM loading rather than direct calls.
db_models.JobPosting.company