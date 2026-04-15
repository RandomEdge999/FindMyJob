from __future__ import annotations

from pathlib import Path

from findmyjob.core.enums import CompanySizeBucket, ExperienceLevel, LocationScope, WorkplaceType
from findmyjob.core.runtime import AppRuntime
from findmyjob.core.types import JobSearchQuery, SavedSearch
from findmyjob.db.repositories import SavedSearchRepository



def test_job_search_query_roundtrip_serializes_structured_filters() -> None:
    query = JobSearchQuery(
        title_keywords=["backend", "platform"],
        keyword="python engineer",
        source_adapter="greenhouse",
        board_token="acme",
        active_only=True,
        locations=["remote", "chicago"],
        countries=["US"],
        regions=["IL"],
        cities=["Chicago"],
        workplace_types=[WorkplaceType.REMOTE],
        employment_types=["full_time"],
        location_scopes=[LocationScope.REMOTE_US],
        experience_levels=[ExperienceLevel.SENIOR],
        company_size_buckets=[CompanySizeBucket.MIDSIZE],
        posted_within_days=14,
        compensation_present=True,
        compensation_min=140000,
        compensation_currency="USD",
        remote_only=True,
        allow_unknown_compensation=True,
        allow_unknown_experience_level=True,
        sponsorship_fit="review_required",
        requires_future_sponsorship=True,
        limit=25,
    )

    payload = query.model_dump(mode="json")
    restored = JobSearchQuery.model_validate(payload)
    discovery = restored.to_discovery_query()

    assert restored == query
    assert discovery.title_keywords == ["backend", "platform"]
    assert discovery.remote_only is True
    assert discovery.compensation_min == 140000



def test_saved_search_repository_crud_and_default_tracking(tmp_path: Path) -> None:
    runtime = AppRuntime.bootstrap(tmp_path)
    query = JobSearchQuery(title_keywords=["backend"], countries=["US"], remote_only=True, experience_levels=[ExperienceLevel.ENTRY_LEVEL], limit=25)

    with runtime.session_scope() as session:
        repo = SavedSearchRepository(session)
        created = repo.save(
            SavedSearch(
                name="remote-us",
                description="Remote US entry roles",
                query_payload=query,
                source_adapter_hint="greenhouse",
                is_default=True,
            )
        )
        created_id = created.id
        assert created.is_default is True
        assert repo.get_default() is not None
        assert repo.to_model(created).query_payload.title_keywords == ["backend"]

    with runtime.session_scope() as session:
        repo = SavedSearchRepository(session)
        updated = repo.save(
            SavedSearch(
                id=created_id,
                name="remote-us",
                description="Updated description",
                query_payload=query.model_copy(update={"countries": ["CA"], "limit": 10}),
                is_default=False,
            )
        )
        repo.rename(updated.id, "north-america-remote")
        repo.mark_default(updated.id)
        repo.touch_last_used(updated.id)
        stored = repo.require(updated.id)
        model = repo.to_model(stored)
        assert model.name == "north-america-remote"
        assert model.query_payload.countries == ["CA"]
        assert model.query_payload.limit == 10
        assert model.is_default is True
        assert model.last_used_at is not None

    with runtime.session_scope() as session:
        repo = SavedSearchRepository(session)
        deleted = repo.delete("north-america-remote")
        assert deleted.name == "north-america-remote"

    with runtime.session_scope() as session:
        repo = SavedSearchRepository(session)
        assert repo.get_by_reference("north-america-remote") is None
        assert repo.get_default() is None
