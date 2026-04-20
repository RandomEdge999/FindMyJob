from __future__ import annotations

from pathlib import Path

from findmyjob.bootstrap import bootstrap_signature, editable_package_spec, should_install_package


def test_editable_package_spec_includes_playwright_extra() -> None:
    assert editable_package_spec(include_playwright=True) == ".[playwright]"
    assert editable_package_spec(include_playwright=False) == "."


def test_bootstrap_signature_tracks_pyproject_and_browser_policy(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='findmyjob'\n", encoding="utf-8")

    signature = bootstrap_signature(tmp_path, include_playwright=True, install_playwright_browser=True)

    assert signature["bootstrap_version"] >= 1
    assert signature["package_spec"] == ".[playwright]"
    assert signature["install_playwright_browser"] is True


def test_should_install_when_package_missing() -> None:
    install_needed, reason = should_install_package(
        {
            "python_exists": True,
            "findmyjob_installed": False,
            "editable_checkout": False,
            "playwright_installed": False,
        },
        stamp_payload=None,
        signature={"bootstrap_version": 1},
        include_playwright=True,
    )

    assert install_needed is True
    assert reason == "package_missing"


def test_should_install_when_bootstrap_stamp_is_stale() -> None:
    install_needed, reason = should_install_package(
        {
            "python_exists": True,
            "findmyjob_installed": True,
            "editable_checkout": True,
            "playwright_installed": True,
        },
        stamp_payload={"bootstrap_version": 1},
        signature={"bootstrap_version": 2},
        include_playwright=True,
    )

    assert install_needed is True
    assert reason == "bootstrap_stamp_stale"


def test_should_skip_install_when_environment_is_current() -> None:
    signature = {"bootstrap_version": 1}
    install_needed, reason = should_install_package(
        {
            "python_exists": True,
            "findmyjob_installed": True,
            "editable_checkout": True,
            "playwright_installed": True,
        },
        stamp_payload=signature,
        signature=signature,
        include_playwright=True,
    )

    assert install_needed is False
    assert reason == "already_ready"