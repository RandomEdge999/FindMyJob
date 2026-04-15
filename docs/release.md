# Release Validation

This document is the release gate for the public repository. It is narrower than the README and focuses on whether the repo is safe to publish and whether a clean install still behaves correctly.

## Supported Public Scope

The current public release posture is:

- Greenhouse-first launch defaults
- safe preview mode by default
- fictional tracked sample candidate data
- local-first operation with ignored runtime state
- LM Studio as the supported local model runtime

Lever and Ashby remain in the codebase, but they are not the default public launch posture.

## Public Boundary

The public repository must not contain:

- `.env` or real credentials
- runtime data under `.fmj/`
- `data/`, `output/`, or `reports/`
- browser profiles or caches
- support bundles or exports
- real candidate source material
- real applications or submissions

The recommended public onboarding surface is a local-only profile file:

- tracked example: `templates/user-profile.local.example.yml`
- live local path: `.fmj/local-overrides/filefirst/user-profile.yml`

The tracked repo should remain in sample mode until a local operator fills that ignored file or uses a deeper local override/onboarding path.

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

`pyproject.toml` remains the canonical dependency source. The public repo also ships `requirements/` convenience exports for pip-based setup.

## Public Launch Validation

On a clean clone with a repo-local Python 3.12 environment:

1. Install Python dependencies.
2. Install Playwright Chromium.
3. Build the frontend.
4. Run `start.ps1` or `start.sh`.
5. Confirm the app serves:
   - `/`
   - `/setup`
   - `/settings`
   - `/autopilot`
   - `/review`
   - `/runs`

The public sample config should start in preview mode. It should not be configured for real submission until the operator explicitly opts in locally.

The Setup page should clearly show whether the workspace is still using tracked sample data or a configured local-only profile.

## Expected Safe Defaults

Release validation should confirm:

- tracked candidate files are fictional
- tracked config defaults do not auto-submit
- helper launchers do not depend on `fmj` being globally installed
- startup scripts require a repo-local Python 3.12 environment
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
