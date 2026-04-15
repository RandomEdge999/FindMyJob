from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import typer
from rich.console import Console

from findmyjob.core.lmstudio import LMSTUDIO_DEFAULT_HOST, probe_lmstudio_base_url
from findmyjob.filefirst import FileWorkspace, build_pdf_for_target, evaluate_target, export_legacy_personal_material, run_pipeline, scan_workspace
from findmyjob.filefirst.llm import LocalGemmaClient

console = Console(width=200)


def _print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, default=str, ensure_ascii=True))
        return
    for key, value in payload.items():
        if isinstance(value, (list, dict)):
            console.print(f"{key}: {json.dumps(value, default=str, ensure_ascii=True)}")
        else:
            console.print(f"{key}: {value}")



def register_filefirst_commands(app: typer.Typer, onboard_app: typer.Typer, models_app: typer.Typer | None = None) -> None:
    screen_app = typer.Typer()
    chatgpt_app = typer.Typer()
    app.add_typer(screen_app, name="screen")
    app.add_typer(chatgpt_app, name="chatgpt")

    @app.command("scan")
    def scan_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        source: str | None = typer.Option(None, help="Limit to a source adapter"),
        board: str | None = typer.Option(None, help="Limit to a single board token"),
        limit: int | None = typer.Option(None, help="Maximum new jobs to persist"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Discover jobs into the file-first inbox."""

        async def _run() -> dict[str, Any]:
            return await scan_workspace(workspace, source=source, board=board, limit=limit)

        result = anyio.run(_run)
        _print_payload(result, json_output=json_output)

    @app.command("eval")
    def eval_command(
        target: str = typer.Argument(..., help="Job ID, application ID, URL, or local file"),
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Evaluate a job target and write a markdown report."""
        result = evaluate_target(workspace, target)
        _print_payload(result, json_output=json_output)

    @app.command("pdf")
    def pdf_command(
        target: str = typer.Argument(..., help="Job ID or application ID"),
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Generate the tailored HTML/PDF packet for a tracked job."""
        result = build_pdf_for_target(workspace, target)
        _print_payload(result, json_output=json_output)

    @app.command("pipeline")
    def pipeline_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        limit: int | None = typer.Option(None, help="Maximum pending jobs to process"),
        skip_pdf: bool = typer.Option(False, "--skip-pdf", help="Evaluate without generating PDFs"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Process pending inbox jobs end-to-end."""
        result = run_pipeline(workspace, limit=limit, generate_pdf=not skip_pdf)
        _print_payload(result, json_output=json_output)

    @app.command("tracker")
    def tracker_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        json_output: bool = typer.Option(False, "--json", help="Emit tracker snapshot as JSON instead of launching TUI"),
    ) -> None:
        """Open the file-first terminal tracker."""
        if json_output:
            from findmyjob.filefirst.tracker import tracker_snapshot

            _print_payload(tracker_snapshot(workspace), json_output=True)
            return
        from findmyjob.filefirst.tracker import launch_tracker

        launch_tracker(workspace)

    @app.command("launch-rehearsal")
    def launch_rehearsal_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        limit: int = typer.Option(5, min=1, max=10, help="Maximum live-market jobs to surface after discovery and screening"),
        job_id: str | None = typer.Option(None, help="Selected job id to run through the full rehearsal"),
        override_rejected: bool = typer.Option(False, "--override-rejected", help="Allow a screened-out job to continue for a manual rehearsal"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Run the live-market rehearsal flow: broad discovery, hard-rule filtering, LM Studio screening, artifact generation, and browser preview without submit."""
        from findmyjob.filefirst.service import FileFirstOperatorService

        service = FileFirstOperatorService(workspace)
        result = service.start_launch_rehearsal(limit=limit) if job_id is None else service.run_launch_rehearsal(job_id=job_id, override_rejected=override_rejected)
        _print_payload(result, json_output=json_output)

    @app.command("launch-rehearsal-interactive")
    def launch_rehearsal_interactive_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        limit: int = typer.Option(10, min=1, max=20, help="How many live-market jobs to discover and screen"),
        auto_pick_suggested: bool = typer.Option(False, "--auto-pick-suggested", help="Immediately run the suggested job if one is available"),
    ) -> None:
        """Discover live jobs, show the screened set, let the operator pick one, then run the preview flow."""
        from findmyjob.filefirst.service import FileFirstOperatorService

        service = FileFirstOperatorService(workspace)
        scan_result = service.start_launch_rehearsal(limit=limit)
        jobs = list(scan_result.get("screened_jobs") or [])
        if not jobs:
            console.print("No live rehearsal candidates were discovered.")
            raise typer.Exit(code=1)

        console.print("\nLive rehearsal candidates:\n")
        suggested_job_id = str(scan_result.get("suggested_job_id") or "").strip()
        default_index = 1
        for index, item in enumerate(jobs, start=1):
            status = str((item.get("screening") or {}).get("status") or "unknown")
            tags = []
            if item.get("rehearsal_eligible"):
                tags.append("eligible")
            if item.get("ats_preview_supported"):
                tags.append(str(item.get("ats_family") or "preview"))
            if item.get("job_id") == suggested_job_id:
                tags.append("suggested")
                default_index = index
            reason = item.get("hard_reject_reason") or item.get("auth_reject_reason") or "; ".join((item.get("screening") or {}).get("reasons") or [])
            console.print(f"[{index}] {item.get('company')} | {item.get('title')}")
            console.print(f"    status={status} tags={', '.join(tags) if tags else '-'}")
            console.print(f"    job_id={item.get('job_id')}")
            if reason:
                console.print(f"    note={reason}")
            console.print(f"    url={item.get('url')}")

        if auto_pick_suggested:
            selected = None
            if suggested_job_id:
                selected = next((item for item in jobs if item.get('job_id') == suggested_job_id), None)
            if selected is None:
                selected = next((item for item in jobs if (item.get('screening') or {}).get('status') in {'approved', 'overridden'} and item.get('rehearsal_eligible')), None)
            if selected is None:
                selected = next((item for item in jobs if item.get('rehearsal_eligible')), None)
            if selected is None:
                selected = jobs[0]
            console.print(f"Auto-picked: {selected.get('company')} | {selected.get('title')}")
        else:
            choice = typer.prompt("Pick job number for preview", default=str(default_index)).strip()
            if not choice.isdigit():
                raise typer.Exit(code=1)
            selected_index = int(choice)
            if selected_index < 1 or selected_index > len(jobs):
                raise typer.Exit(code=1)
            selected = jobs[selected_index - 1]

        if selected is None:
            raise typer.Exit(code=1)
        selected_job_id = str(selected.get('job_id') or '').strip()
        screening_status = str((selected.get('screening') or {}).get('status') or '')
        override = screening_status not in {'approved', 'overridden'}
        result = service.run_launch_rehearsal(job_id=selected_job_id, override_rejected=override)
        while True:
            submission = result.get('submission') or {}
            application_id = str(submission.get('application_id') or '').strip()
            if not application_id:
                break
            details = service.application_detail_payload(application_id)
            required_questions = [
                item
                for item in details.get('questions', [])
                if item.get('required') and item.get('needs_user_input') and item.get('question_type') != 'file'
            ]
            if not required_questions:
                break
            console.print("\nRequired inputs before preview can continue:\n")
            answered_any = False
            for question in required_questions:
                prompt_text = str(question.get('prompt_text') or 'Question').strip()
                existing_answer = str(question.get('existing_answer') or '').strip()
                answer = typer.prompt(prompt_text, default=existing_answer or None).strip()
                if not answer:
                    continue
                service.answer_question(application_id=application_id, question_id=str(question.get('question_id') or ''), answer_text=answer, auto_retry=False)
                answered_any = True
            if not answered_any:
                break
            result = service.run_launch_rehearsal(job_id=selected_job_id, override_rejected=override)
        _print_payload(result, json_output=False)

    @chatgpt_app.command("browser")
    def chatgpt_browser_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        launch: bool = typer.Option(False, "--launch", help="Launch the managed ChatGPT browser profile"),
        close_existing: bool = typer.Option(False, "--close-existing", help="Close all existing Chrome windows before launching"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Inspect or launch the managed ChatGPT drafting browser."""
        from findmyjob.filefirst.chatgpt_drafting import ChatGPTDraftingService

        service = ChatGPTDraftingService(workspace)
        if launch:
            payload = service.launch_browser(close_existing=close_existing)
        else:
            payload = service.status_payload()
        _print_payload(payload, json_output=json_output)

    @app.command("reject")
    def reject_command(
        job_id: str = typer.Argument(..., help="Job ID to reject"),
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        note: str = typer.Option("", help="Optional reason for rejection"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Manually reject a job so it won't be processed further."""
        from findmyjob.filefirst.screening import override_screening

        ws = FileWorkspace(workspace)
        ws.ensure()
        job = ws.load_job(job_id)
        if job is None:
            console.print(f"Job not found: {job_id}")
            raise typer.Exit(code=1)
        updated_job, screening = override_screening(ws, job_id, approved=False, note=note or "Manually rejected by operator")
        ws.update_inbox_state(job_id, "screened_out")
        payload = {
            "job_id": job_id,
            "company": updated_job.company,
            "title": updated_job.title,
            "workflow_state": updated_job.workflow_state,
            "screening_status": screening.status,
            "note": note or "Manually rejected by operator",
        }
        if json_output:
            _print_payload(payload, json_output=True)
        else:
            console.print(f"Rejected: {updated_job.company} | {updated_job.title} ({job_id})")

    @screen_app.command("reset")
    def screen_reset_command(
        job_id: str | None = typer.Argument(None, help="Job ID to reset"),
        all_jobs: bool = typer.Option(False, "--all", help="Reset every screened out job back to pending"),
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Reset screened-out jobs back to pending so they can re-enter the pipeline."""
        from findmyjob.filefirst.screening import reset_screening

        ws = FileWorkspace(workspace)
        ws.ensure()
        if all_jobs:
            job_ids = [
                job.job_id
                for job in ws.load_inbox()
                if str(job.workflow_state or '').strip().lower() == 'screened_out'
            ]
            reset_jobs = [reset_screening(ws, target_job_id) for target_job_id in job_ids]
            payload = {
                'reset': len(reset_jobs),
                'job_ids': [job.job_id for job in reset_jobs],
            }
            _print_payload(payload, json_output=json_output)
            return
        if not job_id:
            console.print("Provide a job ID or use --all.")
            raise typer.Exit(code=1)
        job = reset_screening(ws, job_id)
        payload = {
            'job_id': job.job_id,
            'company': job.company,
            'title': job.title,
            'workflow_state': job.workflow_state,
            'screening_status': None,
        }
        _print_payload(payload, json_output=json_output)

    @onboard_app.command("export-file-first")
    def export_file_first_command(
        workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
        source_dir: Path | None = typer.Option(None, help="Source directory containing personal onboarding files"),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Export personal onboarding material into the file-first workspace."""
        result = export_legacy_personal_material(workspace, source_dir=source_dir)
        _print_payload(result, json_output=json_output)

    if models_app is not None:
        @models_app.command("local-check")
        def local_check_command(
            workspace: Path = typer.Option(Path.cwd(), help="Workspace root"),
            base_url: str | None = typer.Option(None, help=f"Override the LM Studio host (default: {LMSTUDIO_DEFAULT_HOST})"),
            model: str | None = typer.Option(None, help="Override the model id to verify"),
            json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
        ) -> None:
            """Verify the local LM Studio profile with text and JSON round-trips."""
            ws = FileWorkspace(workspace)
            ws.ensure()
            settings = ws.load_profile().runtime.model
            overrides: dict[str, Any] = {}
            if base_url:
                overrides["base_url"] = probe_lmstudio_base_url(base_url).canonical_base_url
            if model:
                overrides["model"] = model
            if overrides:
                settings = settings.model_copy(update=overrides)
            result = LocalGemmaClient(settings).verify()
            _print_payload(result, json_output=json_output)


__all__ = ["register_filefirst_commands"]
