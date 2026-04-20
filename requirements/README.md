# Requirements Exports

`pyproject.toml` is the source of truth for Python dependencies.

This folder exists for contributors who prefer plain `pip install -r ...` flows:

- `base.txt`: runtime install
- `dev.txt`: test and development install
- `playwright.txt`: development install plus browser automation dependencies

Typical setup:

```bash
python -m pip install -r requirements/playwright.txt
python -m playwright install chromium
```

If you are working from a fresh clone, the supported public launchers and `scripts/bootstrap_env.py` can now create `.venv312`, install the editable package, and install Chromium automatically.
