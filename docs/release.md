# Release Validation

This document is the release gate for the public repository. It is narrower than the README and focuses on whether the repo is safe to publish and whether a clean install still behaves correctly.

## Supported Public Scope

The current public release posture is:

- Greenhouse-first launch defaults
- safe preview mode by default
- fictional tracked sample candidate data
- local-first operation with ignored runtime state
- LM Studio as the supported local model runtime
- optional OpenRouter-style `remote_http` bindings for screening and question-answering roles only; writer roles remain LM Studio-local

Lever and Ashby remain in the codebase, but they are not the default public launch posture.

## Licensing Boundary

The public repository must ship with the source-available [PolyForm Noncommercial 1.0.0](../LICENSE) terms and the creator notices in [NOTICE](../NOTICE).

Release validation should confirm that the published posture matches the license:

- personal, educational, evaluation, and internal noncommercial use are allowed
- source modification and self-hosting are allowed for those noncommercial cases
- commercial resale, paid hosted service, and third-party commercial operation are not allowed without separate permission
- README language should describe the project as source-available and noncommercial, not as unrestricted open source

## Public Boundary

The public repository must not contain:

- `.env` or real credentials
- runtime data under `.fmj/`
- `data/`, `output/`, or `reports/`
- browser profiles or caches
- support bundles or exports
- real candidate source material
- real applications or submissions
- owner-only working notes (`claude.md`, `sprint.md`, `docs/sprint*_evidence_*.md`)
- real CV files (`cv.md`, anything under `my_personal_information/`)
- real Custom GPT slug URLs (e.g. `https://chatgpt.com/g/g-...`); only the placeholder URL belongs in tracked defaults
- real personal email addresses, app passwords, or API keys

Gate commands that must return empty:

```powershell
git ls-files | Select-String -Pattern "^\.env$"
git ls-files cv.md profile/answer-memory.yml profile/facts.yml
git log --all --full-history -- .env
```

The recommended public onboarding surface is a local-only profile file:

- tracked example: `templates/user-profile.local.example.yml`
- live local path: `.fmj/local-overrides/filefirst/user-profile.yml`

The tracked repo should remain in sample mode until a local operator fills that ignored file or uses a deeper local override/onboarding path.

The Settings page may export or import a non-personal configuration bundle for portability, but that bundle must continue excluding profile facts, answer memory, dossiers, runtime history, generated outputs, and secret values.

Scratch and verification artifacts should stay under `.tmp/`. Public release validation should not leave behind multiple root-level temp trees.

## Release Validation Commands

```bash
python -m compileall src tests alembic
pytest -q
npm --prefix frontend run build
python -m build --sdist --wheel --no-isolation
python scripts/publish_audit.py
python scripts/verify_release.py
```

`scripts/publish_audit.py` is the privacy and publish-safety gate.

`scripts/verify_release.py` is the clean-install gate. It validates packaging, workspace bootstrap, and bundled web assets in a fresh environment.

It does not replace the manual startup check from `start.ps1` or `start.sh`; those launchers still need to be exercised separately during public-release validation.

Its seeded clean workspace is still expected to report a failing `launch-check` JSON payload because no real local candidate data, browser runtime, or live launch prerequisites are configured yet. That expected `launch-check` failure does not mean the clean-install gate itself failed.

`pyproject.toml` remains the canonical dependency source. The public repo also ships `requirements/` convenience exports for pip-based setup.

## Public Launch Validation

On a clean clone with Python 3.12 installed on the machine:

1. Run `start.ps1` or `start.sh`.
2. Confirm the launcher creates or repairs `.venv312` automatically.
3. Confirm the launcher installs the editable project and Playwright Chromium.
4. Confirm the app serves:
   - `/`
   - `/setup`
   - `/settings`
   - `/autopilot`
   - `/review`
   - `/runs`

`start.bat` may also be used on Windows, but it delegates directly to `start.ps1` and should be treated as the same launch path for validation purposes.

For the managed-install path, also validate this Windows one-liner from outside any checkout:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/RandomEdge999/FindMyJob/main/install.ps1 | iex"
```

And the matching POSIX one-liner on macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/RandomEdge999/FindMyJob/main/install.sh | bash
```

Both installers must:

- prompt for an install location (or accept the per-OS default with `-Yes` / `--yes`)
- clone or fast-forward an existing checkout into `<install-root>/repo`
- write `findmyjob` and `findmyjob-update` launcher shims into `<install-root>/bin`
- update the user PATH (Windows user PATH; macOS/Linux shell rc) so a new terminal can run `findmyjob`
- delegate the actual bootstrap and launch to `start.ps1` (Windows) or `start.sh` (POSIX)
- on a second run, fast-forward in place rather than backing up and re-cloning

Default install roots:

| OS | Default install root |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Programs\FindMyJob` |
| macOS | `~/Library/Application Support/FindMyJob` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/findmyjob` |

The public sample config should start in preview mode. It should not be configured for real submission until the operator explicitly opts in locally.

The Setup page should clearly show whether the workspace is still using tracked sample data or a configured local-only profile.

## Expected Safe Defaults

Release validation should confirm:

- tracked candidate files are fictional
- tracked config defaults do not auto-submit
- helper launchers do not depend on `fmj` being globally installed
- the managed installer can clone or update the repo without requiring the user to prepare a manual checkout first
- startup scripts can bootstrap a repo-local Python 3.12 environment when system Python 3.12 is available
- published package metadata should match that same Python 3.12 launch contract
- frontend assets are buildable and bundled
- `fmj init` seeds a local-only user profile template under `.fmj/local-overrides/filefirst/`

## Pre-Publish Manual Checklist

Before the first public push:

1. Run `python scripts/publish_audit.py`.
2. Confirm `.gitignore` covers runtime, reports, outputs, browser data, and local profiles.
3. Confirm tracked sample files contain no real identity or workstation paths.
4. Confirm `.env` is ignored and `.env.example` contains placeholders only.
5. Confirm the Setup page reports sample mode until a local-only profile is configured.
6. Confirm the web app still boots from `start.ps1` and `start.sh`.
7. Confirm the release bundle passes `scripts/verify_release.py`.
8. Initialize a fresh public Git history only after the hygiene checks are clean.
