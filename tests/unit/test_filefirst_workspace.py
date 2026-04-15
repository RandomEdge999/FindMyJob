from __future__ import annotations

import threading
import time
from pathlib import Path

import yaml
from findmyjob.core.lmstudio import LMSTUDIO_AUTO_MODEL
from findmyjob.filefirst.models import AnswerMemoryEntry
from findmyjob.filefirst.portal_defaults import BOOTSTRAP_PORTAL_BOARDS, bootstrap_board_targets
from findmyjob.filefirst.models import InboxJob
from findmyjob.filefirst.workspace import FileWorkspace


def test_filefirst_workspace_bootstraps_defaults(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()

    assert ws.cv_path.exists()
    assert ws.profile_path.exists()
    assert ws.portals_path.exists()
    assert ws.user_profile_template_path.exists()
    assert ws.inbox_path.exists()
    assert ws.scan_history_path.exists()
    assert ws.applications_path.exists()
    assert (tmp_path / 'modes' / 'eval.md').exists()

    profile = ws.load_profile()
    assert profile.runtime.model.provider == 'lmstudio'
    assert profile.runtime.model.transport == 'local_http'
    assert profile.runtime.model.base_url == 'http://127.0.0.1:1234'
    assert profile.runtime.model.api_key_env is None
    assert profile.runtime.model.model == LMSTUDIO_AUTO_MODEL
    assert profile.runtime.automation.enabled is True
    assert profile.runtime.automation.submit_enabled is False
    assert profile.runtime.automation.ready_to_apply_threshold == 10
    assert profile.runtime.automation.default_submit_mode == 'preview_first'
    assert profile.runtime.automation.production_sources == ['greenhouse']
    assert ws.workspace_config_path.exists()

    portals = ws.load_portals()
    assert portals.sources['greenhouse'].enabled is True
    assert portals.sources['lever'].enabled is False
    assert portals.sources['ashby'].enabled is False
    assert portals.sources['greenhouse'].boards == []
    assert portals.sources['lever'].boards == []
    assert portals.sources['ashby'].boards == []
    assert bootstrap_board_targets() == BOOTSTRAP_PORTAL_BOARDS
    assert ws.board_discovery_path.exists()
    assert set(ws.load_board_discovery_state().sources) == {'greenhouse', 'lever', 'ashby'}
    assert ws.load_inbox() == []
    assert ws.load_applications() == []
    assert ws.user_profile_surface()['mode'] == 'sample_mode'


def test_filefirst_workspace_prefers_ignored_local_override_profile(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.local_profile_path.parent.mkdir(parents=True, exist_ok=True)
    ws.local_profile_path.write_text(
        yaml.safe_dump(
            {
                'candidate': {
                    'name': 'Local Candidate',
                    'email': 'local@example.com',
                    'location': 'Denver, CO, US',
                },
                'runtime': {
                    'automation': {
                        'submit_enabled': True,
                        'default_submit_mode': 'auto_submit',
                    }
                },
            },
            sort_keys=False,
        ),
        encoding='utf-8',
    )

    profile = ws.load_profile()

    assert profile.candidate.name == 'Local Candidate'
    assert profile.candidate.email == 'local@example.com'
    assert profile.runtime.automation.submit_enabled is True
    assert profile.runtime.automation.default_submit_mode == 'auto_submit'


def test_filefirst_workspace_single_local_user_profile_drives_profile_facts_answers_and_cv(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    external_cv = tmp_path / 'private-cv.md'
    external_cv.write_text('# Private CV\n\nBuilt reliable local-first automation.\n', encoding='utf-8')
    ws.user_profile_path.parent.mkdir(parents=True, exist_ok=True)
    ws.user_profile_path.write_text(
        yaml.safe_dump(
            {
                'candidate': {
                    'name': 'Local Candidate',
                    'email': 'local@example.com',
                    'location': 'Denver, CO, US',
                    'target_roles': ['Backend Engineer'],
                },
                'authorization': {
                    'is_authorized': True,
                    'requires_future_sponsorship': False,
                },
                'education': [
                    {
                        'school': 'Local State',
                        'degree': 'B.S.',
                        'field': 'Computer Science',
                    }
                ],
                'languages': [
                    {
                        'name': 'English',
                        'fluency': 'Fluent',
                    }
                ],
                'resume': {
                    'markdown_path': str(external_cv),
                },
                'default_answers': [
                    {
                        'question': 'Are you fluent in English?',
                        'answer': 'Yes, I am fluent in English.',
                    }
                ],
                'facts': {
                    'work': [
                        {
                            'id': 'work.current',
                            'title': 'Backend Engineer',
                            'company': 'Acme',
                            'summary': 'Built local-first automation.',
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding='utf-8',
    )

    profile = ws.load_profile()
    facts = ws.load_facts()
    answers = ws.load_answer_memory()
    cv = ws.load_cv()

    assert profile.candidate.name == 'Local Candidate'
    assert profile.candidate.email == 'local@example.com'
    assert profile.candidate.target_roles == ['Backend Engineer']
    assert any(fact.kind == 'contact' and fact.payload.get('email') == 'local@example.com' for fact in facts)
    assert any(fact.kind == 'authorization' and fact.payload.get('is_authorized') is True for fact in facts)
    assert any(fact.kind == 'education' and fact.payload.get('school') == 'Local State' for fact in facts)
    assert any(fact.kind == 'language' and fact.payload.get('name') == 'English' for fact in facts)
    assert any(fact.kind == 'work' and fact.fact_id == 'work-current' for fact in facts)
    assert any(item.canonical_question == 'Are you fluent in English?' for item in answers)
    assert cv.startswith('# Private CV')
    assert ws.user_profile_surface()['mode'] == 'local_user_profile'


def test_personal_saves_write_to_local_overrides_instead_of_tracked_sample_files(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    original_profile = ws.profile_path.read_text(encoding='utf-8')
    original_facts = ws.facts_path.read_text(encoding='utf-8')
    original_answers = ws.answer_memory_path.read_text(encoding='utf-8')
    original_cv = ws.cv_path.read_text(encoding='utf-8')

    profile = ws.load_profile()
    profile.candidate.name = 'Saved Locally'
    ws.save_profile(profile)
    ws.save_facts([])
    ws.save_answer_memory([])
    ws.save_cv('# Local CV')

    assert ws.local_profile_path.exists()
    assert ws.local_facts_path.exists()
    assert ws.local_answer_memory_path.exists()
    assert ws.local_cv_path.exists()
    assert ws.profile_path.read_text(encoding='utf-8') == original_profile
    assert ws.facts_path.read_text(encoding='utf-8') == original_facts
    assert ws.answer_memory_path.read_text(encoding='utf-8') == original_answers
    assert ws.cv_path.read_text(encoding='utf-8') == original_cv


def test_workspace_preserves_multiple_answer_memory_entries_for_same_question(tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_answer_memory(
        [
            AnswerMemoryEntry(
                canonical_question='are-you-a-veteran-have-you-served-in-the-military',
                context_constraints={},
                answer_text='I am not a protected veteran',
                approved=True,
            ),
            AnswerMemoryEntry(
                canonical_question='are-you-a-veteran-have-you-served-in-the-military',
                context_constraints={
                    'question_type': 'sensitive',
                    'source_adapter': 'greenhouse',
                    'option_signature': 'active duty|military spouse|no military service',
                },
                answer_text='No military service',
                approved=True,
            ),
        ]
    )

    answers = ws.load_answer_memory()
    veteran_answers = [
        item for item in answers
        if item.canonical_question == 'are-you-a-veteran-have-you-served-in-the-military'
    ]

    assert len(veteran_answers) == 2
    assert {item.answer_text for item in veteran_answers} == {
        'I am not a protected veteran',
        'No military service',
    }


def test_bootstrap_board_targets_cover_expanded_seed_lists() -> None:
    assert len(BOOTSTRAP_PORTAL_BOARDS['lever']) == 10
    assert {'plaid', 'aircall', 'metabase', 'whoop', 'hive', 'spotify', 'regrello', 'ivo', 'bumbleinc', 'supermove'}.issubset(BOOTSTRAP_PORTAL_BOARDS['lever'])
    assert len(BOOTSTRAP_PORTAL_BOARDS['ashby']) == 17
    assert {'notion', 'openai', 'replit', 'perplexity', 'modal', 'linear', 'ramp', 'mercor', 'harvey', 'exa', 'krea', 'hud', 'magical', 'runway', 'runway-ml', 'rillet', 'adaptivesecurity'}.issubset(BOOTSTRAP_PORTAL_BOARDS['ashby'])


def test_repo_answer_memory_includes_common_greenhouse_defaults() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    payload = yaml.safe_load((repo_root / 'profile' / 'answer-memory.yml').read_text(encoding='utf-8'))
    canonical_questions = {item['canonical_question'] for item in payload['answers']}

    assert {
        'how-did-you-hear-about-this-job',
        'what-gender-identity-do-you-most-closely-identify-with',
        'are-you-a-person-of-transgender-experience',
        'what-sexual-orientation-do-you-most-closely-identify-with',
        'do-you-live-with-a-disability-as-outlined-by-the-ada',
        'are-you-a-veteran-have-you-served-in-the-military',
        'please-select-up-to-2-ethnicities-that-you-most-closely-identify-with',
    }.issubset(canonical_questions)


def _job(job_id: str, *, workflow_state: str = 'pending') -> InboxJob:
    return InboxJob(
        job_id=job_id,
        company='Acme',
        company_key='acme',
        title=f'Role {job_id}',
        source='greenhouse',
        source_kind='greenhouse',
        source_job_id=job_id,
        url=f'https://boards.greenhouse.io/acme/jobs/{job_id}',
        apply_url=f'https://boards.greenhouse.io/acme/jobs/{job_id}',
        location='Remote',
        description='Build reliable systems.',
        discovered_at='2026-04-10T00:00:00Z',
        workflow_state=workflow_state,
        board_family='greenhouse',
        automation_tier='auto_submit_supported',
        job_identity_key=job_id,
        duplicate_cluster_key=job_id,
    )


def test_upsert_inbox_jobs_serializes_concurrent_writes(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    original_load_inbox = FileWorkspace.load_inbox
    first_call_started = threading.Event()
    first_call = True

    def delayed_load_inbox(self):
        nonlocal first_call
        snapshot = original_load_inbox(self)
        if self is ws and first_call:
            first_call = False
            first_call_started.set()
            time.sleep(0.2)
        return snapshot

    monkeypatch.setattr(FileWorkspace, 'load_inbox', delayed_load_inbox)

    thread_one = threading.Thread(target=ws.upsert_inbox_jobs, args=([_job('job-1')],))
    thread_two = threading.Thread(target=ws.upsert_inbox_jobs, args=([_job('job-2')],))

    thread_one.start()
    assert first_call_started.wait(timeout=1.0)
    thread_two.start()
    thread_one.join()
    thread_two.join()

    assert {job.job_id for job in ws.load_inbox()} == {'job-1', 'job-2'}


def test_update_inbox_state_serializes_concurrent_writes(monkeypatch, tmp_path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.upsert_inbox_jobs([_job('job-1'), _job('job-2')])

    original_load_inbox = FileWorkspace.load_inbox
    first_call_started = threading.Event()
    first_call = True

    def delayed_load_inbox(self):
        nonlocal first_call
        snapshot = original_load_inbox(self)
        if self is ws and first_call:
            first_call = False
            first_call_started.set()
            time.sleep(0.2)
        return snapshot

    monkeypatch.setattr(FileWorkspace, 'load_inbox', delayed_load_inbox)

    thread_one = threading.Thread(target=ws.update_inbox_state, args=('job-1', 'screened_out'))
    thread_two = threading.Thread(target=ws.update_inbox_state, args=('job-2', 'evaluated'))

    thread_one.start()
    assert first_call_started.wait(timeout=1.0)
    thread_two.start()
    thread_one.join()
    thread_two.join()

    states = {job.job_id: job.workflow_state for job in ws.load_inbox()}
    assert states == {'job-1': 'screened_out', 'job-2': 'evaluated'}
