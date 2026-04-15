from __future__ import annotations

import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def collapse_whitespace(value: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "").strip()).strip()


def strip_html_tags(text: str | None) -> str:
    if not text:
        return ""
    stripped = _HTML_TAG_RE.sub(" ", str(text))
    return collapse_whitespace(html.unescape(stripped))


def drop_trailing_single_character_lines(text: str | None) -> str:
    lines = str(text or "").splitlines()
    cleaned: list[str] = []
    single_char_run: list[str] = []

    def flush_run() -> None:
        nonlocal single_char_run
        if 0 < len(single_char_run) < 2:
            cleaned.extend(single_char_run)
        single_char_run = []

    for line in lines:
        if len(line.strip()) == 1:
            single_char_run.append(line)
            continue
        flush_run()
        cleaned.append(line)

    flush_run()
    while cleaned and len(cleaned[-1].strip()) == 1:
        cleaned.pop()
    return "\n".join(cleaned).strip()
