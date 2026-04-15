from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_typst_executable() -> str | None:
    direct = shutil.which("typst")
    if direct:
        return direct
    if os.name != "nt":
        return None

    packages_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if packages_root.exists():
        try:
            for base in sorted(packages_root.glob("Typst.Typst_*")):
                nested = base / "typst-x86_64-pc-windows-msvc" / "typst.exe"
                if nested.exists():
                    return str(nested)
        except OSError:
            pass

    candidates = [
        Path(r"C:\Program Files\RStudio\resources\app\bin\quarto\bin\tools\x86_64\typst.exe"),
        Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "typst.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def find_latex_engine() -> str | None:
    # Prefer XeLaTeX for Unicode-safe launch rendering and fall back to pdfLaTeX.
    return shutil.which("xelatex") or shutil.which("pdflatex")
