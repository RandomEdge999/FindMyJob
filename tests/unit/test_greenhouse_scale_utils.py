from findmyjob.sources.greenhouse_scale import extract_greenhouse_board_tokens, parse_sitemap_urls
from findmyjob.sources.greenhouse_universe import builtin_greenhouse_board_tokens


def test_extract_greenhouse_board_tokens_from_public_urls() -> None:
    text = "https://boards.greenhouse.io/acme https://job-boards.greenhouse.io/example-team and https://boards-api.greenhouse.io/v1/boards/demo-board/jobs"
    assert extract_greenhouse_board_tokens(text) == {"acme", "example-team", "demo-board"}


def test_parse_sitemap_urls_extracts_locations() -> None:
    xml_text = """
    <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
      <url><loc>https://example.com/careers</loc></url>
      <url><loc>https://example.com/jobs/platform</loc></url>
    </urlset>
    """
    assert parse_sitemap_urls(xml_text) == ["https://example.com/careers", "https://example.com/jobs/platform"]


def test_builtin_greenhouse_board_universe_has_seed_tokens() -> None:
    tokens = builtin_greenhouse_board_tokens()
    assert len(tokens) >= 10
    assert "openai" in tokens
    assert len(tokens) == len(set(tokens))
