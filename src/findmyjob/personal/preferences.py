from __future__ import annotations

from pathlib import Path
from typing import Any

from tomlkit import item, parse, table

from findmyjob.core.config import AppConfig, PersonalSettings, write_default_workspace_config
from findmyjob.core.enums import CompanySizeBucket, ExperienceLevel, SponsorshipFit, WorkplaceType
from findmyjob.core.paths import workspace_config_file
from findmyjob.core.types import JobSearchQuery, SavedSearch
from findmyjob.db.repositories import SavedSearchRepository

PERSONAL_PREFERENCE_KEYS = {
    'enabled_saved_search_presets',
    'countries',
    'regions',
    'cities',
    'remote_only',
    'workplace_types',
    'experience_levels',
    'posted_within_days',
    'company_size_buckets',
    'compensation_min',
    'compensation_currency',
    'sponsorship_fit',
    'requires_future_sponsorship',
    'default_result_limit',
    'auto_prepare_after_discovery',
    'allow_unknown_compensation',
    'allow_unknown_experience_level',
}


def effective_enabled_saved_search_presets(personal: PersonalSettings) -> list[str]:
    return list(personal.enabled_saved_search_presets or personal.saved_search_presets)


def describe_personal_preferences(personal: PersonalSettings) -> list[tuple[str, str]]:
    workplace = ', '.join(value.value for value in personal.workplace_types) or '-'
    experience = ', '.join(value.value for value in personal.experience_levels) or '-'
    company_sizes = ', '.join(value.value for value in personal.company_size_buckets) or '-'
    enabled_presets = ', '.join(effective_enabled_saved_search_presets(personal)) or '-'
    configured_enabled = ', '.join(personal.enabled_saved_search_presets) or '-'
    return [
        ('Enabled Presets', enabled_presets),
        ('Configured Enabled Presets', configured_enabled),
        ('Preferred Countries', ', '.join(personal.countries) or '-'),
        ('Preferred Regions', ', '.join(personal.regions) or '-'),
        ('Preferred Cities', ', '.join(personal.cities) or '-'),
        ('Remote Only', _format_optional_bool(personal.remote_only)),
        ('Workplace Types', workplace),
        ('Experience Levels', experience),
        ('Posted Within Days', str(personal.posted_within_days or '-')),
        ('Company Sizes', company_sizes),
        ('Compensation Floor', _format_compensation(personal.compensation_min, personal.compensation_currency)),
        ('Sponsorship Fit', personal.sponsorship_fit.value if personal.sponsorship_fit is not None else '-'),
        ('Needs Sponsorship', _format_optional_bool(personal.requires_future_sponsorship)),
        ('Result Limit', str(personal.default_result_limit or '-')),
        ('Auto Prepare After Discovery', _format_optional_bool(personal.auto_prepare_after_discovery)),
        ('Keep Unknown Compensation', _format_optional_bool(personal.allow_unknown_compensation)),
        ('Keep Unknown Experience', _format_optional_bool(personal.allow_unknown_experience_level)),
    ]


def compose_personal_query(query: JobSearchQuery, personal: PersonalSettings) -> JobSearchQuery:
    merged = query.model_copy(deep=True)
    if personal.countries:
        merged.countries = list(personal.countries)
    if personal.regions:
        merged.regions = list(personal.regions)
    if personal.cities:
        merged.cities = list(personal.cities)
    if personal.remote_only is not None:
        merged.remote_only = personal.remote_only
    if personal.workplace_types:
        merged.workplace_types = list(personal.workplace_types)
    if personal.experience_levels:
        merged.experience_levels = list(personal.experience_levels)
    if personal.posted_within_days is not None:
        merged.posted_within_days = personal.posted_within_days
    if personal.company_size_buckets:
        merged.company_size_buckets = list(personal.company_size_buckets)
    if personal.compensation_min is not None:
        merged.compensation_min = personal.compensation_min
    if personal.compensation_currency:
        merged.compensation_currency = personal.compensation_currency
    if personal.sponsorship_fit is not None:
        merged.sponsorship_fit = personal.sponsorship_fit.value
    if personal.requires_future_sponsorship is not None:
        merged.requires_future_sponsorship = personal.requires_future_sponsorship
    if personal.default_result_limit is not None:
        merged.limit = personal.default_result_limit
    if personal.allow_unknown_compensation is not None:
        merged.allow_unknown_compensation = personal.allow_unknown_compensation
    if personal.allow_unknown_experience_level is not None:
        merged.allow_unknown_experience_level = personal.allow_unknown_experience_level
    return merged


def resolve_personal_saved_searches(runtime) -> list[SavedSearch]:
    names = effective_enabled_saved_search_presets(runtime.config.personal)
    if not names:
        raise ValueError('No enabled personal saved-search presets are configured. Use `fmj personal prefs set --enabled-preset <name>`.')
    with runtime.session_scope() as session:
        repo = SavedSearchRepository(session)
        _sync_builtin_saved_search_presets(repo, names)
        models: list[SavedSearch] = []
        missing: list[str] = []
        invalid_sources: list[str] = []
        for name in names:
            record = repo.get_by_reference(name)
            if record is None:
                missing.append(name)
                continue
            model = repo.to_model(record)
            if model.query.source_adapter not in {None, '', 'greenhouse'}:
                invalid_sources.append(f"{model.name}:{model.query.source_adapter}")
                continue
            models.append(model)
    if missing:
        raise ValueError('Unknown enabled personal preset(s): ' + ', '.join(missing))
    if invalid_sources:
        raise ValueError('Personal daily-run only supports greenhouse saved searches. Invalid preset sources: ' + ', '.join(invalid_sources))
    return models


def reset_personal_preferences(workspace: Path) -> PersonalSettings:
    config_path, doc, personal = _load_personal_doc(workspace)
    for key in sorted(PERSONAL_PREFERENCE_KEYS):
        if key in personal:
            del personal[key]
    config_path.write_text(doc.as_string(), encoding='utf-8')
    return AppConfig.load(workspace).personal


def update_personal_preferences(workspace: Path, *, updates: dict[str, Any], clear_fields: list[str] | None = None) -> PersonalSettings:
    config_path, doc, personal = _load_personal_doc(workspace)
    unknown = sorted(set(clear_fields or []) - PERSONAL_PREFERENCE_KEYS)
    if unknown:
        raise ValueError('Unknown preference field(s): ' + ', '.join(unknown))
    for key in sorted(clear_fields or []):
        if key in personal:
            del personal[key]
    for key, value in updates.items():
        if key not in PERSONAL_PREFERENCE_KEYS:
            raise ValueError(f'Unsupported preference field: {key}')
        _write_preference_value(personal, key, value)
    config_path.write_text(doc.as_string(), encoding='utf-8')
    return AppConfig.load(workspace).personal


def _load_personal_doc(workspace: Path):
    root = workspace.resolve()
    config_path = workspace_config_file(root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding='utf-8'))
    personal = doc.get('personal')
    if personal is None:
        personal = table()
        doc['personal'] = personal
    return config_path, doc, personal


def _sync_builtin_saved_search_presets(repo: SavedSearchRepository, names: list[str]) -> None:
    from findmyjob.personal.onboarding import DEFAULT_PRESET_DEFINITIONS, _preset_query_payload

    builtin_names = [name for name in names if name in DEFAULT_PRESET_DEFINITIONS]
    if not builtin_names:
        return

    default_record = repo.get_default()
    default_name = default_record.name if default_record is not None else None
    for index, name in enumerate(builtin_names):
        definition = DEFAULT_PRESET_DEFINITIONS[name]
        repo.save(
            SavedSearch(
                name=name,
                description=str(definition['description']),
                query_payload=_preset_query_payload(definition),
                is_default=(default_name == name or (default_name is None and index == 0)),
            )
        )


def _write_preference_value(personal, key: str, value: Any) -> None:
    if value is None:
        if key in personal:
            del personal[key]
        return
    if isinstance(value, list):
        rendered = []
        for item_value in value:
            rendered.append(_render_value(item_value))
        personal[key] = item(rendered)
        return
    personal[key] = _render_value(value)


def _render_value(value: Any) -> Any:
    if isinstance(value, (WorkplaceType, ExperienceLevel, CompanySizeBucket, SponsorshipFit)):
        return value.value
    return value


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return '-'
    return 'yes' if value else 'no'


def _format_compensation(amount: int | None, currency: str | None) -> str:
    if amount is None and not currency:
        return '-'
    if amount is None:
        return str(currency or '-')
    if currency:
        return f'{currency.upper()} {amount:,}'
    return f'{amount:,}'
