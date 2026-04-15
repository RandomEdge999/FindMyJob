from __future__ import annotations

from pathlib import Path

from findmyjob.core import tooling


def test_find_typst_executable_falls_back_to_quarto(monkeypatch) -> None:
    quarto_path = Path(r"C:\Program Files\RStudio\resources\app\bin\quarto\bin\tools\x86_64\typst.exe")
    monkeypatch.setattr(tooling.os, 'name', 'nt')
    monkeypatch.setattr(tooling.shutil, 'which', lambda value: None)
    monkeypatch.setattr(tooling.Path, 'exists', lambda self: str(self) == str(quarto_path))

    assert tooling.find_typst_executable() == str(quarto_path)


def test_find_typst_executable_prefers_winget_install(monkeypatch, tmp_path: Path) -> None:
    packages_root = tmp_path / 'AppData' / 'Local' / 'Microsoft' / 'WinGet' / 'Packages' / 'Typst.Typst_Test' / 'typst-x86_64-pc-windows-msvc'
    packages_root.mkdir(parents=True, exist_ok=True)
    typst_path = packages_root / 'typst.exe'
    typst_path.write_text('', encoding='utf-8')

    monkeypatch.setattr(tooling.os, 'name', 'nt')
    monkeypatch.setattr(tooling.shutil, 'which', lambda value: None)
    monkeypatch.setattr(tooling.Path, 'home', staticmethod(lambda: tmp_path))

    assert tooling.find_typst_executable() == str(typst_path)
