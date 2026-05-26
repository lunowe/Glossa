"""Resolve a Source to its raw textual content.

Push sources carry their content inline. Pull sources have a fetch_callback
the host registered when creating the Source; Glossa calls it to retrieve
content lazily, so the host stays the system of record.
"""

import os

import httpx

from glossa.models.source import Source, SourceIngestionMode


class SourceFetchError(RuntimeError):
    pass


async def fetch_content(source: Source, max_chars: int) -> str:
    if source.ingestion_mode == SourceIngestionMode.PUSH:
        content = source.content_inline or ""
    else:
        cb = source.fetch_callback
        if cb is None:
            raise SourceFetchError(f"pull-mode source {source.id} has no fetch_callback")
        headers = dict(cb.headers or {})
        if cb.auth_ref and cb.auth_ref.startswith("env:"):
            token = os.environ.get(cb.auth_ref[4:])
            if token:
                headers.setdefault("Authorization", f"Bearer {token}")
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.request(cb.method, cb.url, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise SourceFetchError(f"fetch_callback for {source.id} failed: {e}") from e
        content = resp.text

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
    return content
