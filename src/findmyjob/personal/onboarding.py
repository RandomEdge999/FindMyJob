from __future__ import annotations

import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import yaml
from pypdf import PdfReader
from tomlkit import item, parse, table

from findmyjob.core.config import AppConfig, write_default_workspace_config
from findmyjob.personal.preferences import effective_enabled_saved_search_presets
from findmyjob.core.enums import ExperienceLevel, FactKind, Sensitivity
from findmyjob.core.paths import ensure_workspace, workspace_config_file
from findmyjob.core.types import JobSearchQuery, ProfileFact, SavedSearch
from findmyjob.db.repositories import ProfileRepository, SavedSearchRepository
from findmyjob.db.session import create_sqlite_engine, make_session_factory, session_scope
from findmyjob.sources.normalizer import normalize_text, slugify

ONBOARDING_FACT_PREFIX = 'onboard.personal'
LOCAL_PROFILE_FACTS_FILE = Path('.fmj/local_profile/profile_facts.yaml')
LOCAL_COVER_LETTER_TEMPLATE = Path('.fmj/local_templates/cover_letter_template.json')
LOCAL_ONBOARDING_MANIFEST = Path('.fmj/onboarding/personal_onboarding.json')
TXT_FILENAME = 'personal_info.txt'
TEX_FILENAME = 'CV_editable.tex'
DOCX_FILENAMES = ('CoverLetter.docx', 'CoverLetter_Sample.docx')
DOCX_FILENAME = DOCX_FILENAMES[0]
PDF_FILENAME = 'personal_data.pdf'
COMMON_LANGUAGE_PATTERNS = ['English', 'Urdu', 'Hindi', 'Punjabi', 'Arabic', 'Korean', 'Spanish', 'French', 'German']
DEFAULT_PRESET_EXPERIENCE_LEVELS = [ExperienceLevel.ENTRY_LEVEL, ExperienceLevel.ASSOCIATE]

COMMON_SKILL_PATTERNS = [
    'Training and evaluation', 'Feature engineering', 'Business Analytics', 'Operations Research', 'Plotly Dash',
    'Machine learning', 'Unit testing', 'Digital Ocean', 'Linux/Unix', 'HTML/CSS', 'C/C++', 'Node.js', 'React.js',
    'Next.js', 'TypeScript', 'JavaScript', 'FastAPI', 'OpenAPI', 'NextAuth', 'TimescaleDB', 'PostgreSQL', 'Postgres',
    'TensorFlow', 'Scikit-learn', 'OpenCV', 'NumPy', 'Pandas', 'PyTorch', 'Plotly', 'Tableau', 'Redis', 'Prisma',
    'Flask', 'Docker', 'Vercel', 'Transformers', 'Webhooks', 'Tailwind', 'Mapbox', 'MATLAB', 'MailChimp', 'WordPress',
    'Excel', 'SQL', 'REST', 'JWT', 'RBAC', 'Validation', 'Logging', 'Dashboards', 'Linters', 'Formatters', 'Blender',
    'Unity', 'Git', 'Python', 'Java', 'SEO',
]
DEFAULT_PRESET_DEFINITIONS: dict[str, dict[str, Any]] = {
    'swe_new_grad_core': {
        'description': 'Entry-level, new grad, and junior software engineering and development roles.',
        'title_keywords': [
            'software engineer', 'entry level software engineer', 'software engineer, new grad',
            'new grad software engineer', 'graduate software engineer', 'junior software engineer',
            'software developer', 'software developer - entry level', 'entry level software developer',
            'junior software developer',
            'software engineer & computer science - recent grad',
            'software engineer & computer science - recent grad/full time',
        ],
    },
    'frontend_web': {
        'description': 'Frontend, web, and UI engineering roles.',
        'title_keywords': [
            'frontend software engineer', 'software engineer - frontend', 'front end developer',
            'junior front end developer', 'ui developer', 'html / markup developer',
            'html developer', 'markup developer', 'web developer',
        ],
    },
    'backend_platform': {
        'description': 'Backend and platform engineering roles.',
        'title_keywords': [
            'junior backend software engineer', 'junior backend developer',
            'backend engineer', 'backend developer', 'platform engineer',
        ],
    },
    'fullstack_mobile': {
        'description': 'Full-stack and mobile engineering roles.',
        'title_keywords': [
            'full stack developer', 'full-stack software engineer', 'full-stack software engineer (new grad)',
            'fullstack engineer', 'mobile developer', 'mobile app developer',
            'mobile software developer', 'ios developer', 'android developer',
        ],
    },
    'ai_ml_data': {
        'description': 'AI, ML, data science, data engineering, and analytics roles.',
        'title_keywords': [
            'machine learning engineer', 'machine learning engineer, new grad',
            'associate ai/machine learning engineer', 'ai/ml engineer', 'ai engineer',
            'artificial intelligence engineer', 'ai developer', 'agentic ai developer',
            'ai-first software engineer', 'applied ai/ml engineer', 'machine learning researcher',
            'entry level data scientist', 'data analyst', 'entry level data analyst',
            'global business data analyst', 'business analyst', 'product analyst',
            'data engineer', 'junior data engineer', 'analytics engineer',
            'analytics engineer (new grad)', 'data analytics engineer',
            'data and analytics engineer', 'business systems analyst',
            'application analyst', 'epic application analyst',
        ],
    },
    'cloud_devops_security': {
        'description': 'Cloud, DevOps, SRE, infrastructure, network, and security roles.',
        'title_keywords': [
            'devops engineer', 'dev ops and cloud engineer', 'dev ops and cloud engineer, associate',
            'devops engineer associate', 'associate, devops engineer', 'site reliability engineer',
            'associate site reliability engineer', 'associate site reliability engineer (sre)',
            'infrastructure site reliability engineer', 'infrastructure site reliability engineer (entry level)',
            'infrastructure and cloud engineer', 'cloud engineer',
            'security analyst', 'information security analyst', 'cybersecurity specialist',
            'jr. security engineer', 'security engineer', 'security engineer, new grad',
            'cloud security engineer', 'network engineer', 'entry level systems engineer',
            'network administrator', 'network specialist',
        ],
    },
    'qa_test': {
        'description': 'QA, test automation, and software testing roles.',
        'title_keywords': [
            'qa tester', 'software tester', 'software test engineer', 'software test (qa) engineer',
            'quality assurance analyst', 'entry level quality assurance analyst', 'qa analyst', 'qa analyst i',
            'quality assurance engineer', 'software quality assurance engineer',
            'qa automation engineer', 'qa automation tester', 'automation tester',
            'automation engineer',
        ],
    },
    'analyst_product': {
        'description': 'Technical support, solutions, implementation, and product management roles.',
        'title_keywords': [
            'technical support specialist', 'technical support engineer',
            'l1 support engineer', 'l1 support engineer - technical', 'application support engineer',
            'solutions engineer', 'implementation engineer', 'solution implementation engineer',
            'associate product manager',
        ],
    },
}
_SECTION_HEADERS = {'Education', 'Experience', 'Uploads', 'Links', 'Skills', 'Languages'}
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_PHONE_RE = re.compile(r'(?:\+?\d[\d()\-\s]{7,}\d)')
_URL_RE = re.compile(r'https?://\S+')
_DATE_RANGE_RE = re.compile(r'(?:[A-Za-z]{3,9}\s+)?\d{4}\s*-\s*(?:Present|(?:[A-Za-z]{3,9}\s+)?\d{4})', re.IGNORECASE)
_LATEX_SECTION_RE = re.compile(r'\\section\{(?P<name>[^}]+)\}(?P<body>.*?)(?=(?:\\section\{|\\end\{document\}))', re.S)
_LATEX_SUBHEADING_RE = re.compile(r'\\resumeSubheading\{(?P<title>.*?)\}\{(?P<dates>.*?)\}\{(?P<org>.*?)\}\{(?P<location>.*?)\}\s*\\resumeItemListStart(?P<body>.*?)\\resumeItemListEnd', re.S)
_LATEX_PROJECT_RE = re.compile(r'\\resumeProject\{(?P<name>.*?)\}\{(?P<dates>.*?)\}\{(?P<subtitle>.*?)\}\s*\\resumeItemListStart(?P<body>.*?)\\resumeItemListEnd', re.S)
_LATEX_ITEM_RE = re.compile(r'\\resumeItem\{(.*?)\}', re.S)


@dataclass(slots=True)
class ParsedPersonalPack:
    contact: dict[str, Any] = field(default_factory=dict)
    location: dict[str, Any] = field(default_factory=dict)
    authorization: dict[str, Any] = field(default_factory=dict)
    educations: list[dict[str, Any]] = field(default_factory=list)
    experiences: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    review_only_facts: list[ProfileFact] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def run_personal_onboarding(workspace: Path, source_dir: Path) -> dict[str, Any]:
    root = workspace.resolve()
    ensure_workspace(root)
    source_root = source_dir.expanduser().resolve()
    paths = _require_personal_sources(source_root)
    parsed = parse_personal_pack(source_root)
    facts = build_profile_facts(parsed)

    facts_path = (root / LOCAL_PROFILE_FACTS_FILE).resolve()
    cover_letter_template_path = (root / LOCAL_COVER_LETTER_TEMPLATE).resolve()
    manifest_path = (root / LOCAL_ONBOARDING_MANIFEST).resolve()
    for directory in {facts_path.parent, cover_letter_template_path.parent, manifest_path.parent}:
        directory.mkdir(parents=True, exist_ok=True)

    cover_letter_template = derive_cover_letter_template(paths['docx'], parsed.contact)
    _write_profile_facts_file(facts_path, facts)
    cover_letter_template_path.write_text(json.dumps(cover_letter_template, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    _write_personal_config(root, source_root, facts_path, cover_letter_template_path, paths['docx'], paths['tex'], manifest_path)

    from findmyjob.core.runtime import AppRuntime

    runtime = AppRuntime.bootstrap(root, config=AppConfig.load(root))
    with runtime.session_scope() as session:
        profile_repo = ProfileRepository(session)
        removed_count = profile_repo.delete_by_fact_id_prefix(ONBOARDING_FACT_PREFIX)
        for fact in facts:
            profile_repo.upsert_fact(fact)
        preset_names = save_default_presets(session)

    manifest = build_onboarding_manifest(
        root=root,
        source_root=source_root,
        facts=facts,
        removed_count=removed_count,
        cover_letter_template_path=cover_letter_template_path,
        manifest_path=manifest_path,
        preset_names=preset_names,
        parsed=parsed,
        resume_template_path=paths['tex'],
        cover_letter_reference_path=paths['docx'],
        facts_path=facts_path,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return manifest


def inspect_personal_onboarding(workspace: Path) -> dict[str, Any]:
    root = workspace.resolve()
    config = AppConfig.load(root)
    manifest_path = config.onboarding_manifest_path(root)
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

    engine = create_sqlite_engine(config.database_path(root))
    session_factory = make_session_factory(engine)
    with session_scope(session_factory) as session:
        facts = [record for record in ProfileRepository(session).list_facts() if str(record.fact_id).startswith(ONBOARDING_FACT_PREFIX)]
    fact_counts = Counter(record.kind.value for record in facts)
    review_only = [
        {'fact_id': record.fact_id, 'kind': record.kind.value, 'category': str((record.payload or {}).get('category') or '')}
        for record in facts
        if record.disallowed or not record.allowed_for_generation
    ]
    return {
        'workspace': str(root),
        'onboarding_enabled': config.personal.enabled,
        'source_dir': str(config.personal_source_dir(root)) if config.personal_source_dir(root) is not None else None,
        'facts_file': str(config.profile_facts_path(root)),
        'fact_counts': dict(sorted(fact_counts.items())),
        'resume_renderer': config.personal.resume_renderer,
        'resume_template': (
            str(config.resume_template_path(root))
            if config.personal.resume_renderer == 'latex' and config.resume_template_path(root) is not None
            else None
        ),
        'resume_source_reference': manifest.get('resume_source_reference'),
        'cover_letter_template': str(config.cover_letter_template_path(root)) if config.cover_letter_template_path(root) is not None else None,
        'cover_letter_reference': str(config.cover_letter_reference_path(root)) if config.cover_letter_reference_path(root) is not None else None,
        'saved_search_presets': list(config.personal.saved_search_presets),
        'enabled_saved_search_presets': list(effective_enabled_saved_search_presets(config.personal)),
        'review_only_facts': review_only,
        'flagged_items': list(manifest.get('flagged_items') or []),
        'skipped_items': list(manifest.get('skipped_items') or []),
        'manifest_path': str(manifest_path),
    }

def parse_personal_pack(source_root: Path) -> ParsedPersonalPack:
    paths = _require_personal_sources(source_root)
    txt = _parse_personal_info_txt(paths['txt'])
    tex = _parse_resume_tex(paths['tex'])
    pdf = _parse_reference_pdf(paths['pdf'])

    contact = _merge_contact(txt['contact'], tex['contact'])
    location = txt.get('location') or tex.get('location') or {}
    authorization = dict(txt.get('authorization') or {})
    flags = list(txt.get('flags') or [])
    skipped = list(txt.get('skipped') or [])

    pdf_email = pdf.get('email')
    if pdf_email and contact.get('email') and pdf_email.lower() != str(contact['email']).lower():
        flags.append('PDF reference contained a different email address; TXT/TEX contact data was kept as source-of-truth.')

    educations = _merge_records(txt['educations'], tex['educations'], key_fields=('school', 'degree'))
    experiences = _merge_experiences(txt['experiences'], tex['experiences'])
    projects = _dedupe_records(tex['projects'], key_fields=('name',))
    skills = _dedupe_preserve_order([
        *txt['skills'],
        *[skill for entry in txt['experiences'] for skill in entry.get('skills', [])],
        *tex['skills'],
        *pdf['skills'],
    ])
    languages = _dedupe_preserve_order([*txt['languages'], *pdf['languages']])

    review_only_facts: list[ProfileFact] = []
    demographic_value = txt.get('demographic_value')
    if demographic_value:
        review_only_facts.append(
            ProfileFact(
                fact_id=f'{ONBOARDING_FACT_PREFIX}.personal.demographic',
                kind=FactKind.PERSONAL,
                payload={'category': 'demographic', 'label': 'race_ethnicity', 'value': demographic_value, 'source_refs': [f'{TXT_FILENAME}:race']},
                sensitivity=Sensitivity.HIGH,
                allowed_for_generation=False,
                disallowed=True,
                provenance='onboarding:txt',
            )
        )
        flags.append('Demographic data was preserved as a review-only personal fact and excluded from auto-fill generation.')

    preferences = {
        'job_role_families': list(DEFAULT_PRESET_DEFINITIONS),
        'saved_search_presets': list(DEFAULT_PRESET_DEFINITIONS),
        'resume_template_strategy': 'retain_local_resume_reference',
        'source_refs': [TXT_FILENAME, TEX_FILENAME],
    }

    return ParsedPersonalPack(
        contact=contact,
        location=location,
        authorization=authorization,
        educations=educations,
        experiences=experiences,
        projects=projects,
        skills=skills,
        languages=languages,
        preferences=preferences,
        review_only_facts=review_only_facts,
        flags=_dedupe_preserve_order(flags),
        skipped=_dedupe_preserve_order(skipped),
        metadata={'source_dir': str(source_root)},
    )


def build_profile_facts(parsed: ParsedPersonalPack) -> list[ProfileFact]:
    facts: list[ProfileFact] = []
    contact_payload = {
        'name': parsed.contact.get('name'),
        'email': parsed.contact.get('email'),
        'phone': parsed.contact.get('phone'),
        'linkedin': parsed.contact.get('linkedin'),
        'github': parsed.contact.get('github'),
        'portfolio': parsed.contact.get('portfolio'),
        'website': parsed.contact.get('website') or parsed.contact.get('portfolio'),
        'source_refs': list(parsed.contact.get('source_refs') or []),
    }
    facts.append(
        ProfileFact(
            fact_id=f'{ONBOARDING_FACT_PREFIX}.contact.primary',
            kind=FactKind.CONTACT,
            payload={key: value for key, value in contact_payload.items() if value},
            sensitivity=Sensitivity.MEDIUM,
            provenance=_provenance_label(contact_payload.get('source_refs') or []),
        )
    )

    if parsed.location:
        facts.append(
            ProfileFact(
                fact_id=f'{ONBOARDING_FACT_PREFIX}.location.primary',
                kind=FactKind.LOCATION,
                payload=parsed.location,
                sensitivity=Sensitivity.MEDIUM,
                provenance=_provenance_label(parsed.location.get('source_refs') or []),
            )
        )

    if parsed.authorization:
        facts.append(
            ProfileFact(
                fact_id=f'{ONBOARDING_FACT_PREFIX}.authorization.us_work',
                kind=FactKind.AUTHORIZATION,
                payload=parsed.authorization,
                sensitivity=Sensitivity.HIGH,
                provenance=_provenance_label(parsed.authorization.get('source_refs') or []),
            )
        )

    facts.append(
        ProfileFact(
            fact_id=f'{ONBOARDING_FACT_PREFIX}.preference.roles',
            kind=FactKind.PREFERENCE,
            payload=parsed.preferences,
            sensitivity=Sensitivity.LOW,
            provenance='onboarding:mixed',
        )
    )

    for entry in parsed.educations:
        school_slug = slugify(f"{entry.get('school', '')}-{entry.get('degree', '')}")
        facts.append(ProfileFact(fact_id=f'{ONBOARDING_FACT_PREFIX}.education.{school_slug}', kind=FactKind.EDUCATION, payload=entry, sensitivity=Sensitivity.LOW, provenance=_provenance_label(entry.get('source_refs') or [])))

    for entry in parsed.experiences:
        work_slug = slugify(f"{entry.get('company', '')}-{entry.get('title', '')}")
        facts.append(ProfileFact(fact_id=f'{ONBOARDING_FACT_PREFIX}.work.{work_slug}', kind=FactKind.WORK, payload=entry, sensitivity=Sensitivity.LOW, provenance=_provenance_label(entry.get('source_refs') or [])))

    for entry in parsed.projects:
        project_slug = slugify(entry.get('name', 'project'))
        facts.append(ProfileFact(fact_id=f'{ONBOARDING_FACT_PREFIX}.project.{project_slug}', kind=FactKind.PROJECT, payload=entry, sensitivity=Sensitivity.LOW, provenance=_provenance_label(entry.get('source_refs') or [])))

    for skill in parsed.skills:
        skill_name = _clean_fact_token(skill)
        if not skill_name:
            continue
        facts.append(ProfileFact(fact_id=f'{ONBOARDING_FACT_PREFIX}.skill.{slugify(skill_name)}', kind=FactKind.SKILL, payload={'name': skill_name, 'category': 'technical', 'summary': skill_name, 'source_refs': [TXT_FILENAME, TEX_FILENAME]}, sensitivity=Sensitivity.LOW, provenance='onboarding:mixed'))

    for language in parsed.languages:
        language_name = _clean_fact_token(language)
        if not language_name:
            continue
        facts.append(ProfileFact(fact_id=f'{ONBOARDING_FACT_PREFIX}.skill.language.{slugify(language_name)}', kind=FactKind.SKILL, payload={'name': language_name, 'category': 'language', 'summary': language_name, 'source_refs': [TXT_FILENAME, PDF_FILENAME]}, sensitivity=Sensitivity.LOW, provenance='onboarding:mixed'))

    facts.extend(parsed.review_only_facts)
    return _dedupe_profile_facts(facts)



def _preset_query_payload(definition: dict[str, Any]) -> JobSearchQuery:
    return JobSearchQuery(
        title_keywords=list(definition['title_keywords']),
        experience_levels=list(definition.get('experience_levels') or DEFAULT_PRESET_EXPERIENCE_LEVELS),
        allow_unknown_experience_level=bool(definition.get('allow_unknown_experience_level', True)),
        limit=int(definition.get('limit') or 50),
    )


def save_default_presets(session) -> list[str]:
    repo = SavedSearchRepository(session)
    has_default = repo.get_default() is not None
    names: list[str] = []
    for index, (name, definition) in enumerate(DEFAULT_PRESET_DEFINITIONS.items()):
        search = SavedSearch(name=name, description=str(definition['description']), query_payload=_preset_query_payload(definition), is_default=(not has_default and index == 0))
        repo.save(search)
        names.append(name)
    return names


def build_onboarding_manifest(*, root: Path, source_root: Path, facts: list[ProfileFact], removed_count: int, cover_letter_template_path: Path, manifest_path: Path, preset_names: list[str], parsed: ParsedPersonalPack, resume_template_path: Path, cover_letter_reference_path: Path, facts_path: Path) -> dict[str, Any]:
    fact_counts = Counter(fact.kind.value for fact in facts)
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'workspace': str(root),
        'source_dir': str(source_root),
        'facts_file': str(facts_path),
        'onboarding_manifest': str(manifest_path),
        'resume_renderer': 'chatgpt_download',
        'resume_source_reference': str(resume_template_path),
        'cover_letter_template': str(cover_letter_template_path),
        'cover_letter_reference': str(cover_letter_reference_path),
        'saved_search_presets': list(preset_names),
        'fact_counts': dict(sorted(fact_counts.items())),
        'review_only_fact_ids': [fact.fact_id for fact in facts if fact.disallowed or not fact.allowed_for_generation],
        'flagged_items': list(parsed.flags),
        'skipped_items': list(parsed.skipped),
        'removed_previous_fact_count': removed_count,
    }


def derive_cover_letter_template(docx_path: Path, contact: dict[str, Any]) -> dict[str, Any]:
    paragraphs = _extract_docx_paragraphs(docx_path)
    company_hint = None
    salutation = 'Dear Hiring Team at {company_name},'
    if paragraphs:
        match = re.match(r'Dear\s+(.+?)\s+Hiring Team,?$', paragraphs[0], flags=re.IGNORECASE)
        if match:
            company_hint = _sanitize_text(match.group(1))
    body: list[str] = []
    signature_started = False
    scrub_values = [str(contact.get('name') or ''), str(contact.get('email') or ''), str(contact.get('phone') or ''), str(contact.get('linkedin') or ''), str(contact.get('github') or ''), str(contact.get('portfolio') or '')]
    for paragraph in paragraphs[1:]:
        cleaned = _sanitize_text(paragraph)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered.startswith('sincerely'):
            signature_started = True
            continue
        if signature_started:
            continue
        if any(value and value.lower() in lowered for value in scrub_values):
            continue
        if company_hint:
            cleaned = cleaned.replace(company_hint, '{company_name}')
        cleaned = re.sub(r'^I am applying for the .+? role(?: in .+?)?\.', 'I am applying for the {job_title} role at {company_name}.', cleaned, count=1)
        body.append(cleaned)
    if not body:
        body = ['I am applying for the {job_title} role at {company_name}.', 'My background fits the responsibilities of this role, and I have attached a resume grounded in my verified experience.', 'Thank you for your consideration.']
    return {'template_version': 'local_cover_letter_v1', 'source_reference': str(docx_path), 'salutation': salutation, 'paragraphs': body, 'closing': 'Sincerely,', 'signature_name': '{name}'}

def _require_personal_sources(source_root: Path) -> dict[str, Path]:
    docx_path = next((source_root / filename for filename in DOCX_FILENAMES if (source_root / filename).exists()), None)
    if docx_path is None:
        legacy_matches = sorted(source_root.glob('CoverLetter*.docx'))
        docx_path = legacy_matches[0] if legacy_matches else source_root / DOCX_FILENAME
    expected = {'txt': source_root / TXT_FILENAME, 'tex': source_root / TEX_FILENAME, 'docx': docx_path, 'pdf': source_root / PDF_FILENAME}
    missing = [str(path) for path in expected.values() if not path.exists()]
    if missing:
        raise FileNotFoundError('Missing personal onboarding source(s): ' + ', '.join(missing))
    return expected


def _write_profile_facts_file(path: Path, facts: list[ProfileFact]) -> None:
    payload = [fact.model_dump(mode='json') for fact in facts]
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding='utf-8')


def _write_personal_config(workspace: Path, source_root: Path, facts_path: Path, cover_letter_template_path: Path, cover_letter_reference_path: Path, resume_template_path: Path, manifest_path: Path) -> None:
    config_path = workspace_config_file(workspace)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        write_default_workspace_config(config_path)
    doc = parse(config_path.read_text(encoding='utf-8'))
    personal = doc.get('personal')
    if personal is None:
        personal = table()
        doc['personal'] = personal
    personal['enabled'] = True
    personal['source_dir'] = _relative_to_workspace(workspace, source_root)
    personal['profile_facts_file'] = _relative_to_workspace(workspace, facts_path)
    personal['onboarding_manifest'] = _relative_to_workspace(workspace, manifest_path)
    personal['resume_renderer'] = 'chatgpt_download'
    if 'resume_template' in personal:
        del personal['resume_template']
    personal['cover_letter_template'] = _relative_to_workspace(workspace, cover_letter_template_path)
    personal['cover_letter_reference'] = _relative_to_workspace(workspace, cover_letter_reference_path)
    personal['saved_search_presets'] = item(list(DEFAULT_PRESET_DEFINITIONS))
    personal['enabled_saved_search_presets'] = item(list(DEFAULT_PRESET_DEFINITIONS))
    config_path.write_text(doc.as_string(), encoding='utf-8')


def _relative_to_workspace(workspace: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(target.resolve())


def _parse_personal_info_txt(path: Path) -> dict[str, Any]:
    text = _normalize_source_text(path.read_text(encoding='utf-8', errors='replace'))
    lines = [_sanitize_text(line) for line in text.splitlines() if _sanitize_text(line)]
    sections = _split_sections(lines)
    root_lines = sections['__root__']
    urls = _classify_urls(_extract_urls(text))
    contact = {
        'name': _name_from_root_lines(root_lines),
        'email': _first_email(text),
        'phone': _normalize_phone(_first_phone(text)),
        **urls,
        'source_refs': [TXT_FILENAME],
    }
    location = _parse_location(_location_from_root_lines(root_lines))
    if location:
        location['source_refs'] = [TXT_FILENAME]

    skipped: list[str] = []
    lowered_text = text.lower()
    authorization: dict[str, Any] = {'source_refs': [TXT_FILENAME]}
    if 'legally authorized to work in us' in lowered_text and 'yes' in lowered_text:
        authorization['is_authorized'] = True
        authorization['country_code'] = 'US'
    if re.search(r'require work visa.*:\s*no', lowered_text) or re.search(r'require sponsorship.*:\s*no', lowered_text):
        authorization['requires_future_sponsorship'] = False
        authorization['country_code'] = 'US'
    if 'choose no for all the ones' in lowered_text:
        skipped.append("Skipped the broad heuristic 'choose no for all the ones'; only explicit authorization facts were imported.")

    demographic_value = None
    for index, line in enumerate(root_lines):
        if line.lower().startswith('-race:'):
            primary = _sanitize_text(line.split(':', 1)[1])
            follow_up = root_lines[index + 1] if index + 1 < len(root_lines) else ''
            normalized = _sanitize_text(re.sub(r'^if they have further option pick\s+', '', follow_up, flags=re.IGNORECASE))
            demographic_value = normalized or primary
            break

    return {
        'contact': contact,
        'location': location,
        'authorization': authorization if len(authorization) > 1 else {},
        'educations': _parse_txt_educations(sections.get('Education', [])),
        'experiences': _parse_txt_experiences(sections.get('Experience', [])),
        'skills': _extract_terms(' '.join(sections.get('Skills', [])), COMMON_SKILL_PATTERNS),
        'languages': _extract_terms(' '.join(sections.get('Languages', [])), COMMON_LANGUAGE_PATTERNS),
        'flags': [],
        'skipped': skipped,
        'demographic_value': demographic_value,
    }


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {'__root__': []}
    current = '__root__'
    for line in lines:
        if line in _SECTION_HEADERS:
            current = line
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _name_from_root_lines(lines: list[str]) -> str | None:
    for index, line in enumerate(lines[:-1]):
        if line.lower() == 'edit':
            candidate = _sanitize_text(lines[index + 1])
            if candidate:
                return candidate
    for line in lines:
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line) or line in _SECTION_HEADERS:
            continue
        if len(line.split()) >= 2 and not line.startswith('-') and 'http' not in line.lower():
            return line
    return None


def _location_from_root_lines(lines: list[str]) -> str | None:
    for line in lines:
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        if ',' in line and any(token in line for token in ('USA', 'United States', ', TN', ', CA', ', NY')):
            return line
    return None


def _parse_location(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parts = [_sanitize_text(part) for part in value.split(',') if _sanitize_text(part)]
    location: dict[str, Any] = {'display': value}
    if parts:
        location['city'] = parts[0]
    if len(parts) > 1:
        location['region_code'] = parts[1]
    if len(parts) > 2:
        country = parts[2]
        location['country_code'] = 'US' if country.lower() in {'usa', 'united states', 'united states of america'} else country
    return location


def _parse_txt_educations(lines: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        school = _sanitize_text(lines[index])
        if not school:
            index += 1
            continue
        cursor = index + 1
        if cursor < len(lines) and lines[cursor] == school:
            cursor += 1
        degree = None
        years = None
        while cursor < len(lines):
            line = _sanitize_text(lines[cursor])
            if _DATE_RANGE_RE.fullmatch(line):
                years = line
                cursor += 1
                break
            if degree is None:
                degree = line
            cursor += 1
        if degree or years:
            start_year, end_year = _parse_year_range(years)
            results.append({'school': school, 'degree': degree, 'start_year': start_year, 'end_year': end_year, 'summary': degree, 'source_refs': [TXT_FILENAME]})
            index = cursor
            continue
        index += 1
    return _dedupe_records(results, key_fields=('school', 'degree'))


def _parse_txt_experiences(lines: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    index = 0
    while index + 3 < len(lines):
        company, title, location_line, dates = lines[index:index + 4]
        if not _looks_like_experience_header(company, title, location_line, dates):
            index += 1
            continue
        cursor = index + 4
        bullets: list[str] = []
        skills: list[str] = []
        while cursor < len(lines):
            line = lines[cursor]
            if _looks_like_next_txt_experience(lines, cursor):
                break
            if line.startswith('- '):
                bullets.append(_sanitize_text(line[2:]))
            elif line.lower().startswith('skills/tools:'):
                skills.extend(_split_csv_like(line.split(':', 1)[1]))
            cursor += 1
        start_label, end_label = _parse_date_range_labels(dates)
        results.append({'company': company, 'title': title, 'location': location_line.split('•', 1)[-1].strip() if '•' in location_line else location_line, 'start_date': start_label, 'end_date': end_label, 'summary': _summarize_bullets(bullets), 'bullets': bullets, 'skills': _dedupe_preserve_order(skills), 'source_refs': [TXT_FILENAME]})
        index = cursor
    return _dedupe_records(results, key_fields=('company', 'title'))


def _looks_like_experience_header(company: str, title: str, location_line: str, dates: str) -> bool:
    if not company or not title or not location_line or not dates:
        return False
    if company.startswith('-') or title.startswith('-') or location_line.startswith('-'):
        return False
    return bool(_DATE_RANGE_RE.fullmatch(_sanitize_text(dates)))


def _looks_like_next_txt_experience(lines: list[str], index: int) -> bool:
    return index + 3 < len(lines) and _looks_like_experience_header(lines[index], lines[index + 1], lines[index + 2], lines[index + 3])

def _parse_resume_tex(path: Path) -> dict[str, Any]:
    text = _normalize_source_text(path.read_text(encoding='utf-8', errors='replace'))
    return {
        'contact': _parse_tex_contact(text),
        'location': {},
        'educations': _parse_tex_educations(text),
        'experiences': [*_parse_tex_experiences(text, 'Research Experience'), *_parse_tex_experiences(text, 'Experience')],
        'projects': _parse_tex_projects(text),
        'skills': _parse_tex_skills(text),
    }


def _parse_reference_pdf(path: Path) -> dict[str, Any]:
    try:
        reader = PdfReader(str(path))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    except Exception:
        text = ''
    text = _normalize_source_text(text)
    return {'email': _first_email(text), 'skills': _extract_terms(text, COMMON_SKILL_PATTERNS), 'languages': _extract_terms(text, COMMON_LANGUAGE_PATTERNS)}


def _parse_tex_contact(text: str) -> dict[str, Any]:
    name_match = re.search(r'\\LARGE\s+\\bfseries\s+([^}]+)', text)
    email_match = re.search(r'\\href\{mailto:([^}]+)\}', text)
    links = _classify_urls([url for url, _label in re.findall(r'\\href\{(https?://[^}]+)\}\{([^}]*)\}', text)])
    return {'name': _sanitize_text(name_match.group(1)) if name_match else None, 'email': email_match.group(1).strip() if email_match else None, 'phone': _normalize_phone(_first_phone(text)), **links, 'source_refs': [TEX_FILENAME]}


def _parse_tex_educations(text: str) -> list[dict[str, Any]]:
    body = _latex_section(text, 'Education')
    results: list[dict[str, Any]] = []
    for match in _LATEX_SUBHEADING_RE.finditer(body):
        school = _latex_to_text(match.group('title'))
        date_label = _latex_to_text(match.group('dates'))
        degree = _latex_to_text(match.group('org'))
        detail = _latex_to_text(match.group('location'))
        start_year, end_year = _parse_year_range(date_label)
        results.append({'school': school, 'degree': degree, 'date_label': date_label, 'start_year': start_year, 'end_year': end_year, 'summary': detail or degree, 'source_refs': [TEX_FILENAME]})
    return _dedupe_records(results, key_fields=('school', 'degree'))


def _parse_tex_skills(text: str) -> list[str]:
    body = _latex_section(text, 'Skills')
    matches = re.findall(r'\\textbf\{([^:}]+):\}\s*(.*?)(?=\\\\|$)', body, flags=re.S)
    skills: list[str] = []
    for _category, raw_items in matches:
        skills.extend(_split_csv_like(_latex_to_text(raw_items)))
    return _dedupe_preserve_order(skills)


def _parse_tex_experiences(text: str, section_name: str) -> list[dict[str, Any]]:
    body = _latex_section(text, section_name)
    results: list[dict[str, Any]] = []
    for match in _LATEX_SUBHEADING_RE.finditer(body):
        title = _latex_to_text(match.group('title'))
        dates = _latex_to_text(match.group('dates'))
        company = _latex_to_text(match.group('org'))
        location = _latex_to_text(match.group('location'))
        bullets = [_latex_to_text(item) for item in _LATEX_ITEM_RE.findall(match.group('body'))]
        start_label, end_label = _parse_date_range_labels(dates)
        results.append({'company': company, 'title': title, 'location': location, 'start_date': start_label, 'end_date': end_label, 'summary': _summarize_bullets(bullets), 'bullets': [bullet for bullet in bullets if bullet], 'skills': [], 'source_refs': [TEX_FILENAME]})
    return _dedupe_records(results, key_fields=('company', 'title'))


def _parse_tex_projects(text: str) -> list[dict[str, Any]]:
    body = _latex_section(text, 'Selected Projects')
    results: list[dict[str, Any]] = []
    for match in _LATEX_PROJECT_RE.finditer(body):
        name = _latex_to_text(match.group('name'))
        date_label = _latex_to_text(match.group('dates'))
        subtitle = _latex_to_text(match.group('subtitle'))
        bullets = [_latex_to_text(item) for item in _LATEX_ITEM_RE.findall(match.group('body'))]
        results.append({'name': name, 'date_label': date_label, 'summary': subtitle or _summarize_bullets(bullets), 'bullets': [bullet for bullet in bullets if bullet], 'source_refs': [TEX_FILENAME]})
    return _dedupe_records(results, key_fields=('name',))


def _latex_section(text: str, section_name: str) -> str:
    for match in _LATEX_SECTION_RE.finditer(text):
        if _sanitize_text(match.group('name')) == section_name:
            return match.group('body')
    return ''


def _latex_to_text(value: str) -> str:
    text = value
    for pattern, replacement in [(r'\\href\{([^}]*)\}\{([^}]*)\}', r'\2'), (r'\\textbf\{([^}]*)\}', r'\1'), (r'\\textit\{([^}]*)\}', r'\1'), (r'\\emph\{([^}]*)\}', r'\1')]:
        text = re.sub(pattern, replacement, text)
    text = text.replace('\\%', '%').replace('\\&', '&').replace('\\_', '_')
    text = re.sub(r'\\[a-zA-Z]+\*?(?:\[[^\]]*\])?', ' ', text)
    text = text.replace('{', ' ').replace('}', ' ').replace('\\', ' ')
    return _sanitize_text(text)


def _extract_docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read('word/document.xml')
    root = ET.fromstring(xml)
    namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    paragraphs: list[str] = []
    for paragraph in root.findall('.//w:p', namespace):
        text_parts = [node.text or '' for node in paragraph.findall('.//w:t', namespace)]
        combined = _sanitize_text(''.join(text_parts))
        if combined:
            paragraphs.append(combined)
    return paragraphs


def _merge_contact(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(secondary)
    merged.update({key: value for key, value in primary.items() if value})
    merged['source_refs'] = _dedupe_preserve_order([*(primary.get('source_refs') or []), *(secondary.get('source_refs') or [])])
    return merged


def _merge_records(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], *, key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*secondary, *primary]:
        key = '|'.join(slugify(str(row.get(field) or '')) for field in key_fields)
        payload = dict(merged.get(key, {}))
        for field, value in row.items():
            if field == 'source_refs':
                payload[field] = _dedupe_preserve_order([*(payload.get(field) or []), *list(value or [])])
            elif isinstance(value, list):
                payload[field] = _dedupe_preserve_order([*(payload.get(field) or []), *value])
            elif value not in {None, ''}:
                payload[field] = value
        merged[key] = payload
    return list(merged.values())


def _merge_experiences(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = _merge_records(primary, secondary, key_fields=('company', 'title'))
    for entry in merged:
        entry['summary'] = entry.get('summary') or _summarize_bullets(entry.get('bullets') or [])
    return merged


def _dedupe_records(records: list[dict[str, Any]], *, key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        key = '|'.join(slugify(str(record.get(field) or '')) for field in key_fields)
        if key not in seen:
            seen[key] = record
        else:
            seen[key] = _merge_records([record], [seen[key]], key_fields=key_fields)[0]
    return list(seen.values())


def _extract_terms(text: str, patterns: list[str]) -> list[str]:
    haystack = _normalize_source_text(text)
    found: list[tuple[int, str]] = []
    for pattern in sorted(patterns, key=len, reverse=True):
        for match in re.finditer(re.escape(pattern), haystack, flags=re.IGNORECASE):
            found.append((match.start(), pattern))
    return _dedupe_preserve_order([label for _position, label in sorted(found, key=lambda item: item[0])])


def _extract_urls(text: str) -> list[str]:
    return [match.rstrip(').,') for match in _URL_RE.findall(text)]


def _classify_urls(urls: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for url in urls:
        lowered = url.lower()
        if 'linkedin.com' in lowered:
            fields['linkedin'] = url
        elif 'github.com' in lowered:
            fields['github'] = url
        elif 'portfolio' in lowered or 'github.io' in lowered:
            fields['portfolio'] = url
        elif 'http' in lowered and 'mailto:' not in lowered:
            fields.setdefault('website', url)
    return fields


def _first_email(text: str) -> str | None:
    match = _EMAIL_RE.search(text or '')
    return match.group(0) if match else None


def _first_phone(text: str) -> str | None:
    match = _PHONE_RE.search(text or '')
    return match.group(0) if match else None


def _normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r'\D+', '', value)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return f'+1 {digits[0:3]} {digits[3:6]} {digits[6:10]}'
    return _sanitize_text(value)


def _normalize_source_text(text: str) -> str:
    cleaned = text
    for source, target in {'\ufeff': '', '\r\n': '\n', '\r': '\n', 'â€¢': '•', 'â€“': '–', 'â€”': '—', 'â€™': "'", 'â€œ': '"', 'â€\x9d': '"', 'Â ': ' ', 'Â': ' '}.items():
        cleaned = cleaned.replace(source, target)
    return cleaned


def _sanitize_text(value: str | None) -> str:
    return normalize_text(str(value or '').replace('\u2022', ' \u2022 ').replace('\xa0', ' '))


def _clean_fact_token(value: str | None) -> str:
    return _sanitize_text(value).strip(' ,;:.()[]{}')


def _dedupe_profile_facts(facts: list[ProfileFact]) -> list[ProfileFact]:
    deduped: dict[str, ProfileFact] = {}
    for fact in facts:
        existing = deduped.get(fact.fact_id)
        if existing is None:
            deduped[fact.fact_id] = fact
            continue
        deduped[fact.fact_id] = ProfileFact(
            fact_id=existing.fact_id,
            kind=existing.kind,
            payload=_merge_fact_payload(existing.payload, fact.payload),
            sensitivity=_max_sensitivity(existing.sensitivity, fact.sensitivity),
            allowed_for_generation=existing.allowed_for_generation and fact.allowed_for_generation,
            disallowed=existing.disallowed or fact.disallowed,
            provenance=existing.provenance if existing.provenance == fact.provenance else 'onboarding:mixed',
            confirmed=existing.confirmed and fact.confirmed,
        )
    return list(deduped.values())


def _merge_fact_payload(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary or {})
    for key, value in (secondary or {}).items():
        if key == 'source_refs':
            merged[key] = _dedupe_preserve_order([*(merged.get(key) or []), *list(value or [])])
        elif isinstance(value, list):
            merged[key] = _dedupe_preserve_order([*(merged.get(key) or []), *value])
        elif value not in {None, ''}:
            merged[key] = value
    return merged


def _max_sensitivity(primary: Sensitivity, secondary: Sensitivity) -> Sensitivity:
    order = {Sensitivity.LOW: 0, Sensitivity.MEDIUM: 1, Sensitivity.HIGH: 2}
    return primary if order[primary] >= order[secondary] else secondary


def _split_csv_like(value: str | None) -> list[str]:
    return [part for part in re.split(r'\s*[;,]\s*', _sanitize_text(value)) if part]


def _summarize_bullets(bullets: list[str]) -> str:
    cleaned = [_sanitize_text(bullet) for bullet in bullets if _sanitize_text(bullet)]
    return ' '.join(cleaned[:2]) if cleaned else ''


def _parse_year_range(label: str | None) -> tuple[int | None, int | None]:
    years = [int(match) for match in re.findall(r'\b(?:19|20)\d{2}\b', label or '')]
    if not years:
        return None, None
    return (None, years[0]) if len(years) == 1 else (years[0], years[-1])


def _parse_date_range_labels(label: str | None) -> tuple[str | None, str | None]:
    parts = [part.strip() for part in str(label or '').split('-', 1)]
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] or None, None)


def _provenance_label(source_refs: list[str]) -> str:
    lowered = {Path(item).suffix.lower() for item in source_refs}
    if lowered == {'.txt'}:
        return 'onboarding:txt'
    if lowered == {'.tex'}:
        return 'onboarding:tex'
    if lowered == {'.docx'}:
        return 'onboarding:docx'
    if lowered == {'.pdf'}:
        return 'onboarding:pdf'
    return 'onboarding:mixed'


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        cleaned = _sanitize_text(value)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen
