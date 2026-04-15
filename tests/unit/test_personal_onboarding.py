from __future__ import annotations

import json
import zipfile
from pathlib import Path

from pypdf import PdfWriter
from typer.testing import CliRunner

from findmyjob.cli.main import app
from findmyjob.core.config import AppConfig
from findmyjob.core.runtime import AppRuntime
from findmyjob.db.repositories import ProfileRepository, SavedSearchRepository
from findmyjob.personal.onboarding import DEFAULT_PRESET_DEFINITIONS, ParsedPersonalPack, build_profile_facts, parse_personal_pack

runner = CliRunner()


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>',
    ]
    for paragraph in paragraphs:
        xml_parts.append(f'<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>')
    xml_parts.append('</w:body></w:document>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', ''.join(xml_parts))


def _write_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open('wb') as handle:
        writer.write(handle)


def create_personal_pack(root: Path) -> Path:
    source_dir = root / 'personal_pack'
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / 'personal_info.txt').write_text(
        """Answers to basic questions:
-related to require work visa (h1b): NO
basically choose no for all the ones and when asked am I legally authorized to work in US say Yes!
-Race: Asian
if they have further option pick Asian

Edit
Test Candidate
Austin, TX, USA
candidate@example.com
+1 512 555 0100
Education
State University
State University
Bachelor's, Computer Science
2022 - 2026
Experience
Acme Labs
Platform Engineering Intern
Acme Labs • Austin, TX, USA
Jun 2025 - Aug 2025
- Built APIs for internal tooling.
- Improved test coverage across services.
Skills/Tools: Python, SQL, Docker
Links
LinkedIn
https://linkedin.com/in/test-candidate
Github
https://github.com/test-candidate
Portfolio
https://example.com/portfolio
Skills
PythonFastAPISQLDockerReact.jsNext.jsMachine learning
Languages
EnglishHindi
""",
        encoding='utf-8',
    )
    (source_dir / 'CV_editable.tex').write_text(
        r"""\documentclass{article}
\usepackage[hidelinks]{hyperref}
\newcommand{\resumeItem}[1]{#1}
\newcommand{\resumeSubheading}[4]{#1 #2 #3 #4}
\newcommand{\resumeProject}[3]{#1 #2 #3}
\newcommand{\resumeItemListStart}{}
\newcommand{\resumeItemListEnd}{}
\begin{document}
\begin{center}
{\LARGE \bfseries Test Candidate}
\small +1 (512) 555-0100 \,|\, \href{mailto:candidate@example.com}{candidate@example.com} \,|\,
\href{https://linkedin.com/in/test-candidate}{linkedin.com/in/test-candidate} \,|\,
\href{https://github.com/test-candidate}{github.com/test-candidate} \,|\,
\href{https://example.com/portfolio}{example.com/portfolio}
\end{center}
\section{Education}
\resumeSubheading{State University}{May 2026}{B.S. in Computer Science}{Algorithms and Systems}
\section{Skills}
{\footnotesize
\textbf{Systems:} FastAPI, Docker, Redis \\
\textbf{Data:} Python, SQL, Pandas \\
}
\section{Experience}
\resumeSubheading{Platform Engineering Intern}{Jun 2025 - Aug 2025}{Acme Labs}{Austin, TX}
\resumeItemListStart
\resumeItem{Built APIs for internal tooling.}
\resumeItem{Improved test coverage across services.}
\resumeItemListEnd
\section{Selected Projects}
\resumeProject{Scheduler App}{2025}{A scheduling platform}
\resumeItemListStart
\resumeItem{Built a scheduling platform with reusable APIs.}
\resumeItemListEnd
\end{document}
""",
        encoding='utf-8',
    )
    _write_docx(
        source_dir / 'CoverLetter.docx',
        [
            'Dear Sample Company Hiring Team,',
            'I am applying for the Associate Engineer role in Austin.',
            'My background fits that blend of engineering and execution.',
            'Thank you for your time and consideration.',
            'Sincerely,',
            'Test Candidate',
            'candidate@example.com',
        ],
    )
    _write_pdf(source_dir / 'personal_data.pdf')
    return source_dir


def test_parse_personal_pack_normalizes_and_merges_sources(tmp_path: Path) -> None:
    source_dir = create_personal_pack(tmp_path)

    parsed = parse_personal_pack(source_dir)

    assert parsed.contact['email'] == 'candidate@example.com'
    assert parsed.contact['github'] == 'https://github.com/test-candidate'
    assert parsed.authorization['is_authorized'] is True
    assert parsed.authorization['requires_future_sponsorship'] is False
    assert parsed.location['city'] == 'Austin'
    assert len(parsed.experiences) == 1
    assert parsed.experiences[0]['company'] == 'Acme Labs'
    assert 'Python' in parsed.skills
    assert 'FastAPI' in parsed.skills
    assert parsed.languages == ['English', 'Hindi']
    assert len(parsed.projects) == 1
    assert parsed.review_only_facts[0].disallowed is True
    assert parsed.preferences['resume_template_strategy'] == 'retain_local_resume_reference'


def test_onboard_personal_command_writes_local_pack_and_presets(tmp_path: Path) -> None:
    source_dir = create_personal_pack(tmp_path)

    result = runner.invoke(app, ['onboard', 'personal', str(source_dir), '--workspace', str(tmp_path)])

    assert result.exit_code == 0, result.output
    config = AppConfig.load(tmp_path)
    assert config.personal.enabled is True
    assert config.personal.resume_renderer == 'chatgpt_download'
    assert config.resume_template_path(tmp_path) is None
    assert config.cover_letter_template_path(tmp_path) is not None
    assert set(config.personal.enabled_saved_search_presets) == set(DEFAULT_PRESET_DEFINITIONS)
    assert config.cover_letter_template_path(tmp_path).exists()
    assert config.profile_facts_path(tmp_path).exists()
    manifest = json.loads(config.onboarding_manifest_path(tmp_path).read_text(encoding='utf-8'))
    assert set(DEFAULT_PRESET_DEFINITIONS) == set(manifest['saved_search_presets'])
    assert manifest['resume_renderer'] == 'chatgpt_download'
    assert Path(manifest['resume_source_reference']) == source_dir / 'CV_editable.tex'

    runtime = AppRuntime.bootstrap(tmp_path, config=config)
    with runtime.session_scope() as session:
        facts = [fact for fact in ProfileRepository(session).list_facts() if fact.fact_id.startswith('onboard.personal')]
        kinds = {fact.kind.value for fact in facts}
        assert {'contact', 'education', 'work', 'skill', 'authorization', 'location', 'preference'} <= kinds
        saved_searches = {item.name: item for item in SavedSearchRepository(session).list_models()}
        assert set(DEFAULT_PRESET_DEFINITIONS) <= set(saved_searches)
        assert saved_searches['swe_new_grad_core'].query_payload.experience_levels == ['entry_level', 'associate']
        assert saved_searches['swe_new_grad_core'].query_payload.allow_unknown_experience_level is True

    inspection = runner.invoke(app, ['onboard', 'inspect', '--json', '--workspace', str(tmp_path)])
    assert inspection.exit_code == 0, inspection.output
    payload = json.loads(inspection.output)
    assert payload['resume_renderer'] == 'chatgpt_download'
    assert payload['resume_template'] is None
    assert Path(payload['resume_source_reference']) == source_dir / 'CV_editable.tex'
    assert set(payload['saved_search_presets']) == set(DEFAULT_PRESET_DEFINITIONS)
    assert set(payload['enabled_saved_search_presets']) == set(DEFAULT_PRESET_DEFINITIONS)
    assert payload['fact_counts']['contact'] == 1


def test_onboard_personal_command_is_idempotent(tmp_path: Path) -> None:
    source_dir = create_personal_pack(tmp_path)

    first = runner.invoke(app, ['onboard', 'personal', str(source_dir), '--workspace', str(tmp_path)])
    second = runner.invoke(app, ['onboard', 'personal', str(source_dir), '--workspace', str(tmp_path)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    manifest = json.loads((tmp_path / '.fmj' / 'onboarding' / 'personal_onboarding.json').read_text(encoding='utf-8'))
    assert manifest['removed_previous_fact_count'] > 0


def test_build_profile_facts_dedupes_slug_collisions() -> None:
    parsed = ParsedPersonalPack(
        contact={'name': 'Test Candidate', 'email': 'candidate@example.com', 'source_refs': ['personal_info.txt']},
        skills=['NumPy', 'NumPy)'],
    )

    facts = build_profile_facts(parsed)
    numpy_facts = [fact for fact in facts if fact.fact_id == 'onboard.personal.skill.numpy']

    assert len(numpy_facts) == 1
    assert numpy_facts[0].payload['name'] == 'NumPy'


def test_gitignore_covers_personal_onboarding_paths() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    gitignore = (repo_root / '.gitignore').read_text(encoding='utf-8')
    for entry in ('my_personal_information/', '.fmj/local_profile/', '.fmj/local_templates/', '.fmj/onboarding/'):
        assert entry in gitignore

