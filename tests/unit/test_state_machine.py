from findmyjob.core.enums import JobLifecycleStatus
from findmyjob.orchestrator.state_machine import can_transition


def test_valid_transition() -> None:
    assert can_transition(JobLifecycleStatus.NORMALIZED, JobLifecycleStatus.CANDIDATE)


def test_invalid_transition() -> None:
    assert not can_transition(JobLifecycleStatus.NORMALIZED, JobLifecycleStatus.SUBMITTED)
