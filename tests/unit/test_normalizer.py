from findmyjob.core.enums import CompensationInterval, ExperienceLevel, LocationScope, WorkplaceType
from findmyjob.sources.normalizer import (
    build_normalized_job,
    duplicate_cluster_key,
    identity_key,
    infer_experience_level,
    normalize_compensation,
    parse_structured_location,
)


def test_identity_key_is_stable() -> None:
    first = identity_key("greenhouse", "openai", "123")
    second = identity_key("greenhouse", "openai", "123")
    assert first == second


def test_duplicate_cluster_key_changes_with_title() -> None:
    first = duplicate_cluster_key("openai", "Software Engineer", "Remote", WorkplaceType.REMOTE, "Build platform tools")
    second = duplicate_cluster_key("openai", "Data Engineer", "Remote", WorkplaceType.REMOTE, "Build platform tools")
    assert first != second


def test_parse_structured_location_handles_remote_us() -> None:
    parsed = parse_structured_location("Remote - United States")
    assert parsed.workplace_type == WorkplaceType.REMOTE
    assert parsed.country_code == "US"
    assert parsed.location_scope == LocationScope.REMOTE_US
    assert parsed.remote_country_codes == ["US"]


def test_parse_structured_location_handles_city_and_region() -> None:
    parsed = parse_structured_location("Chicago, IL")
    assert parsed.workplace_type == WorkplaceType.ONSITE
    assert parsed.city == "Chicago"
    assert parsed.region_code == "IL"
    assert parsed.country_code == "US"
    assert parsed.location_scope == LocationScope.CITY_SPECIFIC


def test_infer_experience_level_is_conservative() -> None:
    inferred = infer_experience_level("Senior Backend Engineer", "Build reliable systems")
    assert inferred.level == ExperienceLevel.SENIOR
    unknown = infer_experience_level("Backend Engineer", "Build reliable systems")
    assert unknown.level == ExperienceLevel.UNKNOWN


def test_normalize_compensation_from_cents_payload() -> None:
    normalized = normalize_compensation(
        [
            {
                "min_cents": 12000000,
                "max_cents": 15000000,
                "currency_type": "USD",
                "pay_input_type": "yearly",
            }
        ]
    )
    assert normalized.minimum == 120000
    assert normalized.maximum == 150000
    assert normalized.currency == "USD"
    assert normalized.interval == CompensationInterval.YEARLY


def test_build_normalized_job_sets_structured_metadata() -> None:
    job = build_normalized_job(
        company_name="OpenAI",
        title="Software Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="123",
        posting_url="https://example.com/jobs/123",
        apply_url="https://example.com/jobs/123/apply",
        location_raw="Remote - United States",
        employment_type="full_time",
        compensation=[{"min_cents": 12000000, "max_cents": 15000000, "currency_type": "USD", "pay_input_type": "yearly"}],
        description="Remote role building reliable systems.",
        posted_at="2026-03-25T12:00:00Z",
    )
    assert job.workplace_type == WorkplaceType.REMOTE
    assert job.country_code == "US"
    assert job.location_scope == LocationScope.REMOTE_US
    assert job.compensation_min == 120000
    assert job.compensation_max == 150000
    assert job.compensation_currency == "USD"
    assert job.compensation_interval == CompensationInterval.YEARLY
    assert job.posted_at is not None
