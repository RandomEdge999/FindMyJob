from __future__ import annotations

from findmyjob.filefirst.source_targets import extract_board_tokens, fallback_targets, normalize_board_token


def test_extract_board_tokens_ignores_html_garbage_after_greenhouse_slug() -> None:
    corpus = 'https://job-boards.greenhouse.io/airtable\n<!doctype html><html lang="en">'

    tokens = extract_board_tokens('greenhouse', corpus)

    assert tokens == {'airtable'}


def test_normalize_board_token_salvages_slug_from_polluted_value() -> None:
    polluted = 'airtable\n<!doctype html><html lang="en">'

    assert normalize_board_token('greenhouse', polluted) == 'airtable'


def test_fallback_targets_filters_invalid_or_polluted_board_tokens() -> None:
    merged = fallback_targets(
        extra={
            'greenhouse': ['figma', 'airtable\n<!doctype html><html>', ''],
            'lever': ['discord"', None],
            'ashby': ['jobs.ashbyhq.com/notion'],
        }
    )

    assert merged == {
        'greenhouse': ['figma', 'airtable'],
        'lever': ['discord'],
        'ashby': ['notion'],
    }
