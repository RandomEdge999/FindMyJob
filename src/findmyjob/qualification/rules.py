from __future__ import annotations

from collections.abc import Sequence

from findmyjob.core.enums import JobLifecycleStatus, SponsorshipFit, SponsorshipSignal
from findmyjob.core.filtering import evaluate_job_against_query
from findmyjob.core.types import EvidenceSnippet, NormalizedJobPosting, ProfileFact, QualificationResult
from findmyjob.sources.contracts import DiscoveryQuery

NO_SPONSORSHIP_PHRASES = (
    "no sponsorship",
    "will not sponsor",
    "unable to sponsor",
    "must be authorized to work",
    "must already be authorized",
    "without sponsorship",
)
YES_SPONSORSHIP_PHRASES = (
    "sponsorship available",
    "will sponsor",
    "can sponsor",
    "visa sponsorship",
)
CPT_PHRASES = ("cpt",)
OPT_PHRASES = ("opt", "stem opt")


def classify_signal(text: str, positive_terms: tuple[str, ...], negative_terms: tuple[str, ...] = ()) -> SponsorshipSignal:
    lowered = text.lower()
    has_positive = any(term in lowered for term in positive_terms)
    has_negative = any(term in lowered for term in negative_terms)
    if has_positive and has_negative:
        return SponsorshipSignal.CONFLICTING
    if has_positive:
        return SponsorshipSignal.EXPLICIT_YES
    if has_negative:
        return SponsorshipSignal.EXPLICIT_NO
    return SponsorshipSignal.UNKNOWN


def qualification_for_job(job: NormalizedJobPosting, query: DiscoveryQuery, facts: Sequence[ProfileFact]) -> QualificationResult:
    score = 0
    reasons: list[str] = []
    evidence: list[EvidenceSnippet] = []

    description_lower = job.normalized_description.lower()
    query_alignment = evaluate_job_against_query(job, query)
    score += query_alignment.score_delta
    reasons.extend(query_alignment.reasons)

    sponsorship_current = classify_signal(description_lower, YES_SPONSORSHIP_PHRASES, NO_SPONSORSHIP_PHRASES)
    sponsorship_future = classify_signal(description_lower, YES_SPONSORSHIP_PHRASES, NO_SPONSORSHIP_PHRASES)
    cpt_support = classify_signal(description_lower, CPT_PHRASES)
    opt_support = classify_signal(description_lower, OPT_PHRASES)

    if sponsorship_current != SponsorshipSignal.UNKNOWN:
        evidence.append(EvidenceSnippet(source="job_description", quote="Sponsorship language detected", confidence=0.8))
    if cpt_support != SponsorshipSignal.UNKNOWN:
        evidence.append(EvidenceSnippet(source="job_description", quote="CPT language detected", confidence=0.8))
    if opt_support != SponsorshipSignal.UNKNOWN:
        evidence.append(EvidenceSnippet(source="job_description", quote="OPT language detected", confidence=0.8))

    needs_future_sponsorship = any(
        fact.kind.value == "authorization" and bool(fact.payload.get("requires_future_sponsorship"))
        for fact in facts
    ) or query.requires_future_sponsorship

    if needs_future_sponsorship and sponsorship_future == SponsorshipSignal.EXPLICIT_NO:
        score -= 100
        reasons.append("Role explicitly rejects sponsorship while profile indicates future sponsorship need")
        decision = JobLifecycleStatus.SCREENED_OUT
        fit = SponsorshipFit.LIKELY_INCOMPATIBLE
    elif sponsorship_future == SponsorshipSignal.EXPLICIT_YES:
        score += 20
        reasons.append("Role explicitly mentions sponsorship support")
        decision = JobLifecycleStatus.CANDIDATE
        fit = SponsorshipFit.LIKELY_COMPATIBLE
    elif needs_future_sponsorship:
        reasons.append("Sponsorship compatibility is unclear")
        decision = JobLifecycleStatus.CANDIDATE if score >= 0 else JobLifecycleStatus.SCREENED_OUT
        fit = SponsorshipFit.REVIEW_REQUIRED
    else:
        decision = JobLifecycleStatus.CANDIDATE if score >= 0 else JobLifecycleStatus.SCREENED_OUT
        fit = SponsorshipFit.REVIEW_REQUIRED if sponsorship_future == SponsorshipSignal.UNKNOWN else SponsorshipFit.LIKELY_COMPATIBLE

    if not query_alignment.matched:
        decision = JobLifecycleStatus.SCREENED_OUT

    confidence = 0.6 if evidence else 0.35
    return QualificationResult(
        score=score,
        decision=decision,
        reasons=reasons,
        sponsorship_current=sponsorship_current,
        sponsorship_future=sponsorship_future,
        cpt_support=cpt_support,
        opt_support=opt_support,
        fit=fit,
        confidence=confidence,
        evidence=evidence,
    )
