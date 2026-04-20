from pathlib import Path

from findmyjob.filefirst.dossier import build_candidate_dossier, regenerate_candidate_dossier
from findmyjob.filefirst.models import FileFact
from findmyjob.filefirst.workspace import FileWorkspace


def test_candidate_dossier_regeneration(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path)
    ws.ensure()
    ws.save_cv('# Test User\n\nBuilt automation systems.\n')
    ws.save_facts(
        [
            FileFact(fact_id='contact.primary', kind='contact', payload={'name': 'Test User', 'email': 'user@example.com'}),
            FileFact(fact_id='work.primary', kind='work', payload={'company': 'Acme', 'title': 'Engineer', 'summary': 'Built operator workflows.'}),
        ]
    )

    dossier = build_candidate_dossier(ws)
    assert 'Candidate Dossier' in dossier
    assert 'Test User' in dossier
    assert 'Built automation systems.' in dossier

    metadata = regenerate_candidate_dossier(ws)
    assert metadata['saved'] is True
    assert ws.load_candidate_dossier() is not None
