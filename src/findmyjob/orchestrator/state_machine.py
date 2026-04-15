from __future__ import annotations

from findmyjob.core.enums import JobLifecycleStatus

ALLOWED_TRANSITIONS: dict[JobLifecycleStatus, set[JobLifecycleStatus]] = {
    JobLifecycleStatus.DISCOVERED: {JobLifecycleStatus.NORMALIZED},
    JobLifecycleStatus.NORMALIZED: {JobLifecycleStatus.DUPLICATE_BLOCKED, JobLifecycleStatus.SCREENED_OUT, JobLifecycleStatus.CANDIDATE},
    JobLifecycleStatus.CANDIDATE: {JobLifecycleStatus.PREPARING, JobLifecycleStatus.SCREENED_OUT},
    JobLifecycleStatus.PREPARING: {JobLifecycleStatus.NEEDS_USER_INPUT, JobLifecycleStatus.READY_FOR_REVIEW, JobLifecycleStatus.FAILED_RETRYABLE, JobLifecycleStatus.FAILED_TERMINAL},
    JobLifecycleStatus.NEEDS_USER_INPUT: {JobLifecycleStatus.PREPARING, JobLifecycleStatus.READY_FOR_REVIEW},
    JobLifecycleStatus.READY_FOR_REVIEW: {JobLifecycleStatus.APPROVED_FOR_SUBMIT, JobLifecycleStatus.FAILED_TERMINAL},
    JobLifecycleStatus.APPROVED_FOR_SUBMIT: {JobLifecycleStatus.SUBMITTING},
    JobLifecycleStatus.SUBMITTING: {JobLifecycleStatus.SUBMITTED, JobLifecycleStatus.SUBMISSION_UNCERTAIN, JobLifecycleStatus.FAILED_RETRYABLE},
    JobLifecycleStatus.FAILED_RETRYABLE: {JobLifecycleStatus.PREPARING, JobLifecycleStatus.SUBMITTING, JobLifecycleStatus.FAILED_TERMINAL},
}


def can_transition(current: JobLifecycleStatus, target: JobLifecycleStatus) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())
