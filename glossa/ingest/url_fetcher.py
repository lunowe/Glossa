"""Fetch a pasted link and convert its readable main content to markdown.

Used by ``url``-mode sources. We fetch the page with ``httpx`` (so timeouts,
redirects and the User-Agent are under our control) and then extract the
readable article body as markdown with ``trafilatura`` — stripping navigation,
ads and boilerplate so the wiki ingests the actual content, not the chrome.

Single page only: we never follow links / crawl.
"""

import asyncio
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from glossa.config import Settings


class UrlFetchError(RuntimeError):
    pass


def _extract_markdown(html: str, url: str) -> str | None:
    """Run trafilatura's readable-content extraction. Imported lazily so the
    dependency is only required when ``url`` sources are actually ingested."""
    try:
        import trafilatura
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise UrlFetchError("url-mode ingestion requires the 'trafilatura' package (pip install trafilatura)") from e

    return trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        favor_recall=True,
    )


async def fetch_url_as_markdown(url: str, *, settings: "Settings") -> str:
    """Fetch ``url`` and return its main content as markdown.

    Raises ``UrlFetchError`` on a network/HTTP failure or when no readable
    content can be extracted from the page.
    """
    headers = {"User-Agent": settings.url_fetch_user_agent}
    async with httpx.AsyncClient(
        timeout=settings.url_fetch_timeout_seconds,
        follow_redirects=True,
        headers=headers,
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise UrlFetchError(f"failed to fetch {url}: {e}") from e

    html = resp.text
    markdown = await asyncio.to_thread(_extract_markdown, html, url)
    if not markdown or not markdown.strip():
        raise UrlFetchError(f"could not extract readable content from {url}")
    return markdown
