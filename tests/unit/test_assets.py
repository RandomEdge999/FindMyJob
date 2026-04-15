from __future__ import annotations

from pathlib import Path

from findmyjob.core.assets import ensure_default_workspace_templates


def test_ensure_default_workspace_templates_copies_bundled_typst_files(tmp_path: Path) -> None:
    created = ensure_default_workspace_templates(tmp_path)

    assert [path.name for path in created] == ["resume.typ", "cover_letter.typ"]
    assert (tmp_path / "templates" / "typst" / "resume.typ").read_text(encoding="utf-8").strip()
    assert (tmp_path / "templates" / "typst" / "cover_letter.typ").read_text(encoding="utf-8").strip()


def test_ensure_default_workspace_templates_preserves_existing_templates(tmp_path: Path) -> None:
    template_dir = tmp_path / "templates" / "typst"
    template_dir.mkdir(parents=True, exist_ok=True)
    existing_resume = template_dir / "resume.typ"
    existing_resume.write_text("custom resume template\n", encoding="utf-8")

    created = ensure_default_workspace_templates(tmp_path)

    assert [path.name for path in created] == ["cover_letter.typ"]
    assert existing_resume.read_text(encoding="utf-8") == "custom resume template\n"
