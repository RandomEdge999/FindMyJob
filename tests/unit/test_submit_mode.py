from pathlib import Path

from findmyjob.filefirst.models import ApplicationEntry, SubmissionRecord
from findmyjob.filefirst.service import FileFirstOperatorService
from findmyjob.filefirst.workspace import FileWorkspace


async def _return_preview(self, application_id, run_id):
    return self.workspace.load_submission(application_id).model_copy(update={'status': 'preview_ready'})


async def _return_submit(self, application_id, run_id):
    return self.workspace.load_submission(application_id).model_copy(update={'status': 'submitted'})


def _seed_ready_submission(tmp_path: Path) -> FileFirstOperatorService:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    profile = ws.load_profile()
    automation = profile.runtime.automation.model_copy(update={'enabled': True, 'submit_enabled': True, 'default_submit_mode': 'auto_submit'})
    ws.save_profile(profile.model_copy(update={'runtime': profile.runtime.model_copy(update={'automation': automation})}))
    ws.upsert_application(ApplicationEntry(id='001', job_id='job-1', date='2026-04-07', company='Acme', role='Backend Engineer', report='reports/001.md', url='https://example.com', source='greenhouse'))
    ws.save_submission(SubmissionRecord(application_id='001', job_id='job-1', company='Acme', role='Backend Engineer', source='greenhouse', status='ready_for_submit', submit_ready=True, plan={'application_url': 'https://example.com', 'source_kind': 'greenhouse', 'fields': [], 'missing_required_fields': [], 'notes': []}))
    return FileFirstOperatorService(ws)


def test_continue_after_ready_prefers_preview(monkeypatch, tmp_path: Path) -> None:
    service = _seed_ready_submission(tmp_path)
    profile = service.workspace.load_profile()
    automation = profile.runtime.automation.model_copy(update={'default_submit_mode': 'preview_first'})
    service.workspace.save_profile(profile.model_copy(update={'runtime': profile.runtime.model_copy(update={'automation': automation})}))
    monkeypatch.setattr(FileFirstOperatorService, '_preview_application_async', _return_preview)
    monkeypatch.setattr(FileFirstOperatorService, '_submit_application_async', _return_submit)

    result = service._continue_after_ready('001', 'run-1')
    assert result.status == 'preview_ready'


def test_continue_after_ready_prefers_submit(monkeypatch, tmp_path: Path) -> None:
    service = _seed_ready_submission(tmp_path)
    monkeypatch.setattr(FileFirstOperatorService, '_preview_application_async', _return_preview)
    monkeypatch.setattr(FileFirstOperatorService, '_submit_application_async', _return_submit)

    result = service._continue_after_ready('001', 'run-1')
    assert result.status == 'submitted'
