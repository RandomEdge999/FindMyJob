from __future__ import annotations

from pathlib import Path

from findmyjob.filefirst.migration import export_legacy_personal_material
from findmyjob.filefirst.workspace import FileWorkspace


MINIMAL_PERSONAL_INFO = """Test User
test@example.com
+1 (555) 111-2222
https://linkedin.com/in/test-user
https://github.com/test-user
Dallas, TX, US

Education
Example University
B.S. Computer Science
2020 - 2024

Experience
Acme
Backend Engineer
Remote • United States
2024 - Present
- Built backend APIs for internal automation.
Skills/Tools: Python, FastAPI, SQL

Skills
Python, FastAPI, SQL

Languages
English
"""

MINIMAL_TEX = r"""
\LARGE \bfseries Test User
\href{mailto:test@example.com}{test@example.com}
\href{https://linkedin.com/in/test-user}{linkedin.com/in/test-user}
\href{https://github.com/test-user}{github.com/test-user}

\section{Experience}
\resumeSubheading{Backend Engineer}{2024 - Present}{Acme}{Remote}
\resumeItemListStart
\resumeItem{Built backend APIs for internal automation.}
\resumeItemListEnd

\section{Selected Projects}
\resumeProject{Career Ops}{2024}{Local-first job search tooling}
\resumeItemListStart
\resumeItem{Built a local job evaluation workflow.}
\resumeItemListEnd

\section{Skills}
\textbf{Languages:} Python, FastAPI, SQL \\
\end{document}
"""


def _write_personal_pack(source_dir: Path) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / 'personal_info.txt').write_text(MINIMAL_PERSONAL_INFO, encoding='utf-8')
    (source_dir / 'CV_editable.tex').write_text(MINIMAL_TEX, encoding='utf-8')
    (source_dir / 'CoverLetter.docx').write_text('placeholder', encoding='utf-8')
    (source_dir / 'personal_data.pdf').write_text('placeholder', encoding='utf-8')



def test_export_legacy_personal_material_writes_filefirst_artifacts(tmp_path) -> None:
    source_dir = tmp_path / 'my_personal_information'
    _write_personal_pack(source_dir)

    result = export_legacy_personal_material(tmp_path, source_dir=source_dir)
    ws = FileWorkspace(tmp_path)

    assert result['facts_exported'] > 0
    assert ws.cv_path.exists()
    assert ws.facts_path.exists()
    assert ws.profile_path.exists()
    assert 'Test User' in ws.load_cv()

    profile = ws.load_profile()
    assert profile.candidate.name == 'Test User'
    assert profile.candidate.email == 'test@example.com'
    assert profile.candidate.summary
    assert profile.candidate.target_roles

    facts = ws.load_facts()
    assert any(fact.kind == 'contact' for fact in facts)
    assert any(fact.kind == 'work' for fact in facts)
