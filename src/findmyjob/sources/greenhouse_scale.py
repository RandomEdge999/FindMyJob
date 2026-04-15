
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import anyio
import httpx

GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"}
CAREER_KEYWORDS = ("career", "careers", "job", "jobs", "join", "work-with-us", "positions", "openings")
BOARD_PATTERNS = (
    "boards.greenhouse.io/",
    "job-boards.greenhouse.io/",
    "boards-api.greenhouse.io/v1/boards/",
)


@dataclass(slots=True)
class BoardCandidate:
    board_token: str
    source_url: str
    discovery_method: str
    source_domain: str | None = None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        href = values.get("href")
        if href:
            self.links.append(href)


class AsyncHostRateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.requests_per_second = max(requests_per_second, 0.1)
        self.interval = 1.0 / self.requests_per_second
        self._locks: dict[str, anyio.Lock] = {}
        self._last_request: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        lock = self._locks.setdefault(host, anyio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last_request.get(host)
            if last is not None and now - last < self.interval:
                await anyio.sleep(self.interval - (now - last))
            self._last_request[host] = time.monotonic()


def extract_greenhouse_board_tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens: set[str] = set()
    for marker in BOARD_PATTERNS:
        start = 0
        while True:
            index = lowered.find(marker, start)
            if index == -1:
                break
            suffix = lowered[index + len(marker) :]
            token = []
            for char in suffix:
                if char.isalnum() or char in {"-", "_"}:
                    token.append(char)
                else:
                    break
            if token:
                tokens.add("".join(token).strip("-_"))
            start = index + len(marker)
    return {token for token in tokens if token}


def parse_links(html: str, base_url: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(html)
    links: list[str] = []
    for href in parser.links:
        if href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        links.append(absolute)
    return links


def career_like_links(links: list[str], seed_host: str) -> list[str]:
    selected: list[str] = []
    for link in links:
        parsed = urlparse(link)
        haystack = f"{parsed.netloc}{parsed.path}".lower()
        if parsed.netloc != seed_host:
            continue
        if any(keyword in haystack for keyword in CAREER_KEYWORDS):
            selected.append(link)
    return selected


def parse_sitemap_urls(xml_text: str) -> list[str]:
    urls: list[str] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return urls
    for element in root.iter():
        if element.tag.endswith("loc") and element.text:
            urls.append(element.text.strip())
    return urls

class GreenhouseScaleClient:
    def __init__(self, *, request_timeout_seconds: int = 30, requests_per_second: float = 1.0, crawl_depth: int = 2) -> None:
        self.request_timeout_seconds = request_timeout_seconds
        self.crawl_depth = crawl_depth
        self.rate_limiter = AsyncHostRateLimiter(requests_per_second)
        self._robots: dict[str, RobotFileParser] = {}
        self.request_count = 0
        self.rate_limited_count = 0

    def reset_stats(self) -> None:
        self.request_count = 0
        self.rate_limited_count = 0

    def stats(self) -> dict[str, int]:
        return {"request_count": self.request_count, "rate_limited_count": self.rate_limited_count}

    async def validate_board(self, client: httpx.AsyncClient, board_token: str) -> Any:
        return await self._get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board_token}")

    async def fetch_board_jobs(self, client: httpx.AsyncClient, board_token: str) -> Any:
        return await self._get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs", params={"content": "true"})

    async def fetch_job_detail(self, client: httpx.AsyncClient, board_token: str, job_id: str) -> dict[str, Any]:
        return await self._get_json(
            client,
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}",
            params={"questions": "true", "pay_transparency": "true"},
        )

    async def discover_boards(
        self,
        client: httpx.AsyncClient,
        *,
        seed_urls: list[str],
        seed_domains: list[str],
        max_boards: int,
        max_pages_per_host: int = 32,
    ) -> list[BoardCandidate]:
        queue: deque[tuple[str, int, str]] = deque()
        visited: set[str] = set()
        pages_per_host: dict[str, int] = {}
        found: dict[str, BoardCandidate] = {}

        for domain in seed_domains:
            root = domain if domain.startswith("http") else f"https://{domain.strip('/')}"
            queue.append((root, 0, urlparse(root).netloc))
            sitemap_url = f"{root.rstrip('/')}/sitemap.xml"
            try:
                sitemap_text = await self._get_text(client, sitemap_url)
            except Exception:
                sitemap_text = ""
            for url in parse_sitemap_urls(sitemap_text):
                if len(queue) >= max_pages_per_host:
                    break
                queue.append((url, 1, urlparse(root).netloc))

        for url in seed_urls:
            absolute = url if url.startswith("http") else f"https://{url.strip('/')}"
            queue.append((absolute, 0, urlparse(absolute).netloc))

        while queue and len(found) < max_boards:
            url, depth, seed_host = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            host = urlparse(url).netloc
            pages_per_host[host] = pages_per_host.get(host, 0) + 1
            if pages_per_host[host] > max_pages_per_host:
                continue
            if not await self._allowed(client, url):
                continue
            try:
                html = await self._get_text(client, url)
            except Exception:
                continue
            for token in extract_greenhouse_board_tokens(f"{url}\n{html}"):
                found.setdefault(token, BoardCandidate(board_token=token, source_url=url, discovery_method="crawl", source_domain=host))
            if depth >= self.crawl_depth:
                continue
            for link in career_like_links(parse_links(html, url), seed_host):
                if link not in visited:
                    queue.append((link, depth + 1, seed_host))
        return list(found.values())

    async def _allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = self._robots.get(robots_url)
        if parser is None:
            parser = RobotFileParser()
            try:
                text = await self._get_text(client, robots_url, allow_missing=True)
            except Exception:
                text = ""
            parser.set_url(robots_url)
            parser.parse(text.splitlines())
            self._robots[robots_url] = parser
        return parser.can_fetch("FindMyJobBot", url)

    async def _get_json(self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._request(client, url, params=params)
        return response.json()

    async def _get_text(self, client: httpx.AsyncClient, url: str, allow_missing: bool = False) -> str:
        response = await self._request(client, url, allow_missing=allow_missing)
        return response.text

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(4):
            await self.rate_limiter.wait(url)
            try:
                self.request_count += 1
                response = await client.get(url, params=params, timeout=self.request_timeout_seconds)
                if response.status_code == 429:
                    self.rate_limited_count += 1
                    retry_after = float(response.headers.get("Retry-After") or 0.0)
                    await anyio.sleep(max(retry_after, 1.0 + attempt))
                    continue
                if allow_missing and response.status_code == 404:
                    return response
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    break
                await anyio.sleep(1.0 + attempt)
        if last_error is None:
            raise RuntimeError(f"Request failed without error: {url}")
        raise last_error

