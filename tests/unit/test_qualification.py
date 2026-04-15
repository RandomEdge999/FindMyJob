from datetime import datetime, timezone

from findmyjob.core.enums import ExperienceLevel, FactKind, JobLifecycleStatus, LocationScope, Sensitivity, SponsorshipFit, SponsorshipSignal, WorkplaceType
from findmyjob.core.types import NormalizedJobPosting, ProfileFact
from findmyjob.qualification.rules import qualification_for_job
from findmyjob.sources.contracts import DiscoveryQuery


def test_explicit_no_sponsorship_screens_out_when_future_sponsorship_needed() -> None:
    job = NormalizedJobPosting(
        company_name="Example",
        company_key="example",
        title="Software Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="1",
        posting_url="https://example.com/job/1",
        apply_url="https://example.com/job/1/apply",
        location_raw="Remote",
        location_normalized="remote",
        workplace_type=WorkplaceType.REMOTE,
        employment_type="full_time",
        compensation=None,
        description="Candidates must already be authorized to work in the US. We will not sponsor visas.",
        normalized_description="Candidates must already be authorized to work in the US. We will not sponsor visas.",
        discovered_at=datetime.now(timezone.utc),
        job_identity_key="abc",
        duplicate_cluster_key="def",
    )
    facts = [
        ProfileFact(
            fact_id="auth-1",
            kind=FactKind.AUTHORIZATION,
            payload={"requires_future_sponsorship": True},
            sensitivity=Sensitivity.HIGH,
        )
    ]
    query = DiscoveryQuery(title_keywords=["software engineer"], locations=["remote"], workplace_types=[WorkplaceType.REMOTE])
    result = qualification_for_job(job, query, facts)
    assert result.decision == JobLifecycleStatus.SCREENED_OUT
    assert result.fit == SponsorshipFit.LIKELY_INCOMPATIBLE
    assert result.sponsorship_future == SponsorshipSignal.EXPLICIT_NO


def test_structured_filter_mismatch_screens_out_job() -> None:
    job = NormalizedJobPosting(
        company_name="Example",
        company_key="example",
        title="Senior Software Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="2",
        posting_url="https://example.com/job/2",
        apply_url="https://example.com/job/2/apply",
        location_raw="Chicago, IL",
        location_normalized="chicago-il",
        country_code="US",
        region_code="IL",
        city="Chicago",
        location_scope=LocationScope.CITY_SPECIFIC,
        workplace_type=WorkplaceType.ONSITE,
        employment_type="full_time",
        experience_level=ExperienceLevel.SENIOR,
        compensation=None,
        description="Backend systems role.",
        normalized_description="backend systems role",
        discovered_at=datetime.now(timezone.utc),
        job_identity_key="ghi",
        duplicate_cluster_key="jkl",
    )
    query = DiscoveryQuery(countries=["CA"], experience_levels=[ExperienceLevel.ENTRY_LEVEL])
    result = qualification_for_job(job, query, [])
    assert result.decision == JobLifecycleStatus.SCREENED_OUT
    assert any("Country did not match" in reason for reason in result.reasons)
    assert any("Experience level did not match" in reason for reason in result.reasons)
