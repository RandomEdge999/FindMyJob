# FindMyJob

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Web_UI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-Frontend-61DAFB?logo=react&logoColor=06131f)](https://react.dev/)
[![Playwright](https://img.shields.io/badge/Playwright-Browser_Automation-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![LM Studio](https://img.shields.io/badge/LM%20Studio-Local_Model_Runtime-111827)](https://lmstudio.ai/)
[![Status](https://img.shields.io/badge/Status-Beta_Launch-orange)](#beta-launch-posture)

FindMyJob is a local-first job application operator for people who want a visible, configurable workflow instead of a black-box "AI apply bot."

It discovers jobs, screens them with a local model, drafts tailored application documents, fills forms in a real browser, and exposes the entire pipeline through a web UI built for an operator to supervise, approve, and recover edge cases.

> **Beta launch posture**
>
> This repository is in **beta**. The current public release is intentionally conservative:
> - Greenhouse is the default launch path
> - public defaults are preview-first and submit-disabled
> - tracked candidate data is fictional
> - real runtime state stays local and ignored
> - more board coverage, validation, and operational hardening will continue to be added

## What FindMyJob Does

- Discovers jobs from configured sources
- Screens and ranks them with a local LM Studio model
- Generates tailored resumes and cover letters
- Tracks drafting, preparation, review, blockers, and submission state
- Fills supported ATS forms in a real browser with visible operator control
- Stores local runtime state without requiring a hosted backend
- Ships a web UI for setup, settings, autopilot, review, and runs

## Current Product Posture

| Area | Current Public Posture |
| --- | --- |
| Primary source | Greenhouse-first |
| Submission default | `preview_first` |
| Public submit default | Disabled |
| Candidate data in repo | Fictional sample only |
| Local model runtime | LM Studio |
| Drafting browser | Dedicated Chrome/Chromium CDP session |
| Operating model | Local-first, single-operator workflow |

Lever and Ashby remain in the codebase, but they are **not** the default public launch path in this beta release.

## Why The Public Repo Is Safe By Default

This repository is designed so the public GitHub version can stay clean while your real local setup keeps working.

Tracked content includes:

- source code
- frontend source and bundled assets
- launch scripts
- docs
- fictional sample candidate files
- reproducible dependency metadata

Tracked content must **not** include:

- `.env` files or secrets
- real personal profile data
- browser profiles or ChatGPT sessions
- runtime state under `.fmj/`
- `data/`, `output/`, or `reports/`
- generated resumes or cover letters
- live applications, submissions, or support bundles

## Technology Stack

- **Backend:** Python, Typer CLI, FastAPI
- **Frontend:** React
- **Browser automation:** Playwright
- **Local model runtime:** LM Studio
- **Persistence:** local workspace files and local database state
- **Packaging:** `pyproject.toml` with optional pip-friendly requirement exports

## Supported Launch Paths

Use one of these public entrypoints:

- Windows PowerShell: [start.ps1](start.ps1)
- Windows CMD / double-click: [start.bat](start.bat)
- Git Bash on Windows: [start.sh](start.sh)

These are the supported launch scripts. They:

- require a repo-local Python 3.12 environment
- run LM Studio preflight
- build the frontend unless explicitly skipped
- launch the web app on `127.0.0.1:8765`
- use the current local workspace in the repo root

`start.sh` is supported from **Git Bash on Windows**. It is not a Linux or WSL launcher.

## Quick Start

### 1. Create a repo-local Python 3.12 environment

PowerShell:

```powershell
py -3.12 -m venv .venv312
.venv312\Scripts\python -m pip install --upgrade pip
.venv312\Scripts\python -m pip install -e ".[dev,playwright]"
.venv312\Scripts\python -m playwright install chromium
```

Git Bash:

```bash
py -3.12 -m venv .venv312
.venv312/Scripts/python.exe -m pip install --upgrade pip
.venv312/Scripts/python.exe -m pip install -e ".[dev,playwright]"
.venv312/Scripts/python.exe -m playwright install chromium
```

### 2. Start LM Studio

FindMyJob expects LM Studio at:

`http://127.0.0.1:1234/v1`

LM Studio is used for:

- screening
- evaluation support
- answer generation for unresolved application questions

### 3. Create your local-only user profile

The public repo ships a tracked example:

- [templates/user-profile.local.example.yml](templates/user-profile.local.example.yml)

Copy it to your ignored local path:

- `.fmj/local-overrides/filefirst/user-profile.yml`

That single local profile is the recommended onboarding surface for public users.

### 4. Launch the app

PowerShell:

```powershell
.\start.ps1
```

Git Bash:

```bash
./start.sh
```

If the browser does not open automatically, visit:

`http://127.0.0.1:8765`

## Configure For Yourself

The tracked profile files in this repo are sample data only. Your real data should stay local.

Recommended local config surface:

- `.fmj/local-overrides/filefirst/user-profile.yml`

That file can cover:

- contact info and links
- target roles and locations
- work authorization
- education
- languages
- resume or CV paths
- key default application answers
- optional structured facts

Advanced local override paths are still supported:

- `.fmj/local-overrides/filefirst/config/profile.yml`
- `.fmj/local-overrides/filefirst/profile/facts.yml`
- `.fmj/local-overrides/filefirst/profile/answer-memory.yml`
- `.fmj/local-overrides/filefirst/cv.md`

Precedence is:

1. ignored local override files and workspace state
2. tracked fictional sample files
3. built-in defaults

That means the GitHub repo can remain public-safe without deleting your real local setup.

## Web UI Workflow

The web UI is the primary operator surface.

### Setup

Use **Setup** to validate:

- local profile status
- source readiness
- browser readiness
- LM Studio readiness

### Settings

Use **Settings** to configure:

- source posture
- drafting behavior
- automation policy
- local runtime preferences

### Autopilot

Use **Autopilot** to:

- reset operational runtime state
- discover jobs
- run a full workflow
- monitor batch progress and blockers

### Review

Use **Review** to inspect:

- queued jobs
- application state
- blockers
- manual handoff cases

### Runs

Use **Runs** to inspect:

- live run state
- recent outcomes
- drafting / prepare / submit transitions
- failure notes

## Dependency Options

`pyproject.toml` is the canonical Python dependency source.

The repo also ships pip-friendly convenience exports in [requirements/](requirements):

- `requirements/base.txt`
- `requirements/dev.txt`
- `requirements/playwright.txt`

Example:

```bash
python -m pip install -r requirements/playwright.txt
python -m playwright install chromium
```

The frontend dependency lock is:

- `frontend/package-lock.json`

## ChatGPT Drafting

ChatGPT drafting uses a dedicated managed browser session over CDP.

Important notes:

- the app can launch the dedicated drafting browser automatically
- you still need to log into ChatGPT once in that dedicated profile
- the public repo does not ship a reusable personal Custom GPT URL
- public tracked defaults use a placeholder GPT URL; you must replace it with your own Custom GPT link
- the Custom GPT should be configured with your own source materials, templates, and profile/background files
- public defaults stay safe; real automation should be enabled deliberately

### Custom GPT Setup

If you want to use `chatgpt_download`, create your own Custom GPT and set its link in:

- `.fmj/config.toml` under `[chatgpt_drafting].gpt_url`
- or the web UI under **Settings → ChatGPT Drafting**

The original GPT was configured with uploaded background/profile material plus editable resume and cover-letter source files. Public users should upload their own equivalents.

Paste this into the Custom GPT **Instructions** field:

```text
You are a high-precision resume and cover letter generation specialist.

Your job is to generate two final deliverables for each provided job description:
1. A fully tailored, one-page resume as a downloadable PDF
2. A fully tailored, one-page cover letter as a downloadable PDF

Use the uploaded templates, background materials, resume source files, and profile facts as the source of truth. You may strengthen phrasing, targeting, and positioning, but do not invent major employers, degrees, titles, dates, or core qualifications. If a detail is not supported by the provided materials, omit it rather than fabricate it.

Your objective is to maximize ATS match quality and interview likelihood while keeping everything credible, polished, and submission-ready.

Output format is strict. Return the final response in exactly this structure and nothing else:

[[PDF_OUTPUT_READY]]
<downloadable resume PDF attachment or link>
<downloadable cover letter PDF attachment or link>
[[PDF_OUTPUT_COMPLETE]]

Hard output rules:
- Output only the two completion markers and the two downloadable PDF attachments or links.
- Do not include any explanation, commentary, summary, markdown, labels, bullet points, headings, code, LaTeX, draft text, or status notes.
- Do not display the resume or cover letter in plain text.
- Do not output anything before [[PDF_OUTPUT_READY]].
- Do not output anything after [[PDF_OUTPUT_COMPLETE]].
- Do not emit the ready marker until both PDFs are fully generated and downloadable.
- Never return only one file.
- Do not ask clarifying questions; complete the task from the provided materials.

Resume requirements:
- Tailor the resume directly to the job description.
- Optimize strongly for ATS keyword alignment, but keep wording natural and credible.
- Make every bullet relevant, specific, and impact-oriented.
- Keep the resume exactly one page.
- Ensure clean professional formatting with balanced spacing, no awkward wrapping, no isolated single words, and no visual clutter.
- The final file must be polished and immediately ready to submit.
- The file name must follow this exact pattern:
  FirstName_LastName_Company_Role_Resume.pdf

Cover letter requirements:
- Tailor the cover letter directly to the same job description.
- Make it natural, confident, specific, and human.
- Clearly connect the candidate’s background to the role.
- Ensure every sentence adds value.
- Keep the cover letter exactly one page.
- Use the current date in Memphis, Tennessee at the top.
- Ensure clean, polished, submission-ready formatting.
- The file name must follow this exact pattern:
  FirstName_LastName_Company_Role_Cover_Letter.pdf

File-generation requirements:
- Both deliverables must be returned as real downloadable PDF artifacts, not plain text descriptions of files.
- The resume artifact must clearly correspond to the resume file.
- The cover letter artifact must clearly correspond to the cover letter file.
- Ensure the rendered file names include the exact suffixes _Resume.pdf and _Cover_Letter.pdf.

Behavioral enforcement:
- Keep all internal reasoning hidden.
- If any internal retries are needed, do them silently.
- Only return the final two downloadable PDF artifacts between the exact markers.
- Always end with [[PDF_OUTPUT_COMPLETE]] once both files are ready.
```

## Real Submission

The public repo starts in safe preview mode:

- `submit_enabled = false`
- `default_submit_mode = preview_first`
- human review is still part of the default posture

Before enabling real submission locally, you should:

- review your local profile and facts
- confirm your answer memory
- validate your target sources
- verify ChatGPT drafting and browser readiness
- explicitly opt into real submission on your own machine

## Scratch, Temp, And Runtime Data Policy

This repo separates source code from temporary and runtime artifacts.

- repo scratch and verification output should live under `.tmp/`
- local runtime state lives under `.fmj/`
- generated operational data should stay in ignored local directories

Public users should not need to clean random root-level temp trees by hand. The repo is being hardened toward a single ignored scratch root, and this beta release already treats `.tmp/` as the preferred scratch area for verification and test flows.

## Repository Layout

```text
src/           Python application code
frontend/      React frontend source
templates/     tracked public templates and examples
config/        tracked sample config
profile/       tracked fictional sample profile data
scripts/       verification and helper tooling
docs/          release and project documentation
.fmj/          local-only workspace state (ignored)
.tmp/          scratch / verification output (ignored)
```

## Release Validation

Run these before publishing:

```powershell
python -m compileall src tests alembic scripts
pytest -q
npm --prefix frontend run build
python -m build --sdist --wheel --no-isolation
python scripts/publish_audit.py
python scripts/verify_release.py
```

What these gates cover:

- **`publish_audit.py`**
  - secret-like tracked content
  - accidental local-identity leaks
  - machine-specific absolute paths
  - tracked operator/runtime artifacts
- **`verify_release.py`**
  - clean package install
  - bundled frontend presence
  - workspace bootstrap
  - public-safe default config

For stricter release expectations, see [docs/release.md](docs/release.md).

## Beta Launch Limitations

This is a beta launch, not a claim of universal ATS coverage.

Current limitations include:

- Greenhouse is the most mature public path
- some non-default boards still need more hardening
- real-world browser automation always has edge cases
- more testing, board coverage, and recovery logic will continue to be added

That is intentional. The repo is being launched in a transparent state: usable, real, local-first, and still actively being hardened.

## Internal Helpers

The supported public launch surface is:

- `start.ps1`
- `start.bat`
- `start.sh`

Additional helper scripts under `scripts/` are convenience tooling, not a second public bootstrap contract.

## Publish Checklist

Before the first public push:

1. Run `python scripts/publish_audit.py`
2. Verify tracked sample files contain no real identity or machine-specific data
3. Confirm `.env` remains local-only and `.env.example` contains placeholders only
4. Confirm the app boots from `start.ps1` or `start.sh`
5. Confirm the Setup page shows sample mode until a local profile is configured
6. Confirm no runtime/output/report/browser artifacts are staged
7. Create a fresh public Git history only after the hygiene checks are clean

## License And Project Status

If you publish this repository, keep the README honest: this is a **beta launch** with a real local workflow, not a finished universal apply engine.

The value of the project is its visible operator model, reproducible setup, and local-first design. More implementations and deeper testing are expected to follow.
