from __future__ import annotations

from pathlib import Path

import pytest

from findmyjob.filefirst.drafting import build_resume_plan_with_router
from findmyjob.filefirst.models import EvaluationResult, FileFact, InboxJob
from findmyjob.filefirst.workspace import FileWorkspace


def _seed_workspace(tmp_path: Path) -> tuple[FileWorkspace, InboxJob, EvaluationResult]:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv("# Test User\n\nBackend engineer.\n")
    ws.save_facts(
        [
            FileFact(fact_id="contact.primary", kind="contact", payload={"name": "Test User", "email": "user@example.com"}),
            FileFact(fact_id="work.primary", kind="work", payload={"summary": "Built backend automation."}),
            FileFact(fact_id="project.primary", kind="project", payload={"summary": "Created a local-first job workspace."}),
            FileFact(fact_id="skill.python", kind="skill", payload={"name": "Python"}),
        ]
    )
    job = InboxJob(
        job_id="job-100",
        company="Acme",
        company_key="acme",
        title="Backend Engineer",
        source="greenhouse",
        source_kind="greenhouse",
        source_job_id="100",
        url="https://boards.greenhouse.io/acme/jobs/100",
        apply_url="https://boards.greenhouse.io/acme/jobs/100",
        description="Build reliable backend systems for AI workflows.",
        board_family="greenhouse",
        automation_tier="auto_submit_supported",
        job_identity_key="job-100",
        duplicate_cluster_key="acme-backend-engineer",
    )
    evaluation = EvaluationResult(
        job_id="job-100",
        company="Acme",
        role="Backend Engineer",
        source="greenhouse",
        url=job.url,
        score=4.5,
        grade="A",
        summary="Strong fit.",
    )
    return ws, job, evaluation


def test_drafting_passes_deterministic_validation_without_repair(monkeypatch, tmp_path: Path) -> None:
    ws, job, evaluation = _seed_workspace(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr("findmyjob.filefirst.drafting.load_model_router", lambda workspace: object())

    async def fake_generate(router, roles, prompt, *, system_prompt):
        _ = (router, roles, prompt, system_prompt)
        calls.append("writer")
        return (
            {
                "resume_draft": {
                    "headline": "Backend engineer for local AI workflows",
                    "summary_lines": ["Built reliable backend services.", "Shipped local automation tooling."],
                    "selected_work_fact_ids": ["work.primary"],
                    "selected_project_fact_ids": ["project.primary"],
                    "selected_skill_fact_ids": ["skill.python"],
                    "custom_bullets": ["Highlight local-first backend delivery."],
                },
                "cover_letter_draft": {
                    "paragraphs": ["p1", "p2", "p3", "p4"],
                },
                "adaptation_summary": "Targeted backend fit.",
            },
            "writer-primary",
        )

    monkeypatch.setattr("findmyjob.filefirst.drafting._generate_json_with_first_role", fake_generate)

    _plan, metadata = build_resume_plan_with_router(ws, job, evaluation)

    assert calls == ["writer"]
    assert metadata["validation_profile"] == "deterministic_validation"
    assert metadata["validation_issues"] == []
    assert metadata["repair_attempted"] is False
    assert metadata["verified"] is True
    assert "verifier_profile" not in metadata
    assert "verifier_issues" not in metadata


def test_drafting_repairs_once_when_deterministic_validation_fails(monkeypatch, tmp_path: Path) -> None:
    ws, job, evaluation = _seed_workspace(tmp_path)
    calls: list[str] = []
    payloads = [
        (
            {
                "resume_draft": {
                    "headline": "Backend engineer",
                    "summary_lines": ["line 1", "line 2", "line 3", "line 4", "line 5"],
                    "selected_work_fact_ids": ["work.primary"],
                    "selected_project_fact_ids": ["project.primary"],
                    "selected_skill_fact_ids": ["skill.python"],
                    "custom_bullets": ["TODO replace bullet"],
                },
                "cover_letter_draft": {
                    "paragraphs": ["p1", "p2", "p3"],
                },
            },
            "writer-primary",
        ),
        (
            {
                "resume_draft": {
                    "headline": "Backend engineer",
                    "summary_lines": ["line 1", "line 2"],
                    "selected_work_fact_ids": ["work.primary"],
                    "selected_project_fact_ids": ["project.primary"],
                    "selected_skill_fact_ids": ["skill.python"],
                    "custom_bullets": ["Grounded bullet"],
                },
                "cover_letter_draft": {
                    "paragraphs": ["p1", "p2", "p3", "p4"],
                },
            },
            "writer-repair",
        ),
    ]

    monkeypatch.setattr("findmyjob.filefirst.drafting.load_model_router", lambda workspace: object())

    async def fake_generate(router, roles, prompt, *, system_prompt):
        _ = (router, roles, prompt, system_prompt)
        calls.append("writer")
        return payloads[len(calls) - 1]

    monkeypatch.setattr("findmyjob.filefirst.drafting._generate_json_with_first_role", fake_generate)

    _plan, metadata = build_resume_plan_with_router(ws, job, evaluation)

    assert calls == ["writer", "writer"]
    assert metadata["repair_attempted"] is True
    assert metadata["repair_writer_profile"] == "writer-repair"
    assert metadata["validation_issues"] == []
    assert metadata["verified"] is True
    assert "verifier_profile" not in metadata
    assert "verifier_issues" not in metadata


def test_drafting_returns_unverified_draft_after_failed_repair(monkeypatch, tmp_path: Path) -> None:
    ws, job, evaluation = _seed_workspace(tmp_path)
    invalid_payload = (
        {
            "resume_draft": {
                "headline": "Backend engineer",
                "summary_lines": ["line 1", "line 2", "line 3", "line 4", "line 5"],
                "custom_bullets": ["[redacted-name]"],
            },
            "cover_letter_draft": {"paragraphs": ["p1", "p2", "p3"]},
        },
        "writer-primary",
    )

    monkeypatch.setattr("findmyjob.filefirst.drafting.load_model_router", lambda workspace: object())

    async def fake_generate(router, roles, prompt, *, system_prompt):
        _ = (router, roles, prompt, system_prompt)
        return invalid_payload

    monkeypatch.setattr("findmyjob.filefirst.drafting._generate_json_with_first_role", fake_generate)

    _plan, metadata = build_resume_plan_with_router(ws, job, evaluation)
    assert metadata["verified"] is False
    assert metadata["repair_attempted"] is True
    assert len(metadata["validation_issues"]) > 0
