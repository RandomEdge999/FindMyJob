# AGENTS.md

Operating contract for AI coding agents and future maintainers working on this
repository. Read this before making changes.

## The user-vs-owner boundary

FindMyJob has two intended audiences. Every change must respect both.

1. **End user.** Someone who runs the one-line installer (`install.ps1` on
   Windows, `install.sh` on macOS or Linux), opens `http://127.0.0.1:8765`,
   completes `/setup`, and uses the app. They should never need to touch git,
   pip, npm, virtualenvs, the filesystem, or any owner-only file.
2. **Maintainer / repo owner.** Someone with a local clone, who edits code,
   runs tests, and pushes changes. They use `start.ps1`, `start.bat`, or
   `start.sh` directly from the checkout.

When you make a change, always ask: does this change preserve the user
experience for someone who has never seen this codebase? If a change would
require a fresh user to read source code or copy files by hand, it belongs in
Setup or Settings, not in the README user path.

## What must never be committed

The following are owner-only and live behind `.gitignore`. Do not stage,
commit, or reference these by absolute path in tracked code:

- `.env` and any real secrets, app passwords, API keys, or session tokens
- `my_personal_information/` — real CV LaTeX, contact details, signatures
- `cv.md` — owner CV scratchpad
- `profile/` — `answer-memory.yml`, `facts.yml`
- `data/`, `output/`, `reports/`, `tempbase/`, `test-temp/` — runtime artifacts
- `.fmj/` — runtime state, browser profiles, ChatGPT sessions, local
  overrides, ledgers, downloads
- `claude.md`, `sprint.md`, `docs/sprint*_evidence_*.md` — owner-only working
  notes
- `app-flow-opus/` — secondary owner-only frontend prototype
- `tmp_*` files — scratch outputs from manual portal testing
- `verify_output*.txt` — local verification scratch
- Real ChatGPT custom GPT slug URLs (e.g. `https://chatgpt.com/g/g-xxxxxxxxx-...`).
  Use placeholder URLs in tracked code; real URLs go in the user's local
  `.fmj/config.toml` via `/settings`.

If you find any of these tracked, untrack with `git rm --cached <path>` and
add an explicit `.gitignore` rule. If they appear in git history, document the
need for `git filter-repo --path <path> --invert-paths` plus force-push.

## Where Custom GPT URLs and LLM keys live

The three Custom GPTs are owner-specific values, not source code. They live in
the user's gitignored runtime config:

- `chatgpt_drafting.gpt_url` — Resume + Cover Letter Writer
- `chatgpt_drafting.screening_url` — Job Screening
- `chatgpt_drafting.qa_url` — Job Application Answering

Storage path: `.fmj/config.toml`. UI surface: **Settings → ChatGPT Drafting**
(`frontend/src/routes/settings.tsx`). Backend route:
`POST /api/settings/chatgpt-drafting`. Schema:
`ChatGPTDraftingSettings` in `src/findmyjob/core/config.py`. Request model:
`ChatGPTDraftingSettingsRequest` in `src/findmyjob/web/routes/api.py`.

LLM API keys (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`) live in `.env`. The
template is `.env.example`. Never echo a key value back from the API.

## Setup vs Settings

- **Setup** (`/setup`, `frontend/src/routes/setup.tsx`) is the first-run
  profile capture surface. It writes the basic candidate profile to the
  workspace. Keep it minimal — identity, contact, target roles, locations,
  authorization.
- **Settings** (`/settings`, `frontend/src/routes/settings.tsx`) is the
  system-config surface. Provider routing, automation policy, ChatGPT URLs,
  portals, and other operational knobs live here.

Do not duplicate fields between Setup and Settings. If a field truly belongs
to first-run identity, put it in Setup. If it is a system knob the user might
revisit, put it in Settings.

## Cross-platform installer contract

The managed installers (`install.ps1`, `install.sh`) must:

- prompt for an install location with a per-OS default
- accept `--install-dir <path>` (sh) / `-InstallRoot <path>` (ps1) for
  non-interactive runs
- accept `--yes` (sh) / `-Yes` (ps1) to skip the prompt
- clone to `<install-root>/repo` via git when available, otherwise download
  the GitHub archive (zip on Windows, tar.gz on POSIX)
- write `findmyjob` and `findmyjob-update` shims into `<install-root>/bin`
- update the user PATH (Windows user PATH; POSIX shell rc)
- delegate the actual env/build/launch work to `start.ps1` or `start.sh`

Default install roots:

| OS | Default |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Programs\FindMyJob` |
| macOS | `~/Library/Application Support/FindMyJob` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/findmyjob` |

The installers must be thin. All bootstrap, build, and launch logic lives in
`scripts/bootstrap_env.py`, `src/findmyjob/bootstrap.py`, `start.ps1`, and
`start.sh`. Do not re-implement those flows inside the installers.

## Pre-commit checklist for agents

Before staging changes, verify:

1. `git ls-files | grep -E '^\.env$'` is empty.
2. `git ls-files cv.md profile/answer-memory.yml profile/facts.yml` is empty.
3. `git status` does not stage anything from `data/`, `output/`, `reports/`,
   `my_personal_information/`, `app-flow-opus/`, or `tmp_*`.
4. No real GPT URL slug (`chatgpt.com/g/g-...`), real email address, or real
   API key appears in tracked files. Public placeholders only.
5. `pytest -q` passes.
6. `python -m findmyjob build --workspace . --json` returns
   `build_status: ready`.
7. `cd frontend && npm run build` succeeds.
