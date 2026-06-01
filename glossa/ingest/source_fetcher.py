"""Resolve a Source to its raw textual content.

Each ingestion mode supplies text differently:

* **push** — content carried inline on the Source.
* **pull** — fetched from the host's ``fetch_callback`` (host stays system of
  record).
* **url** — the pasted link is fetched and its readable main content converted
  to markdown.
* **upload** — the raw file stored at ``source.asset_path`` is parsed to text
  with LiteParse.

Everything downstream of this function (entity extraction, page merge, …) sees a
plain string regardless of mode.
"""

import os
from typing import TYPE_CHECKING

import httpx

from glossa.ingest import doc_parser, url_fetcher
from glossa.models.source import Source, SourceIngestionMode

if TYPE_CHECKING:
    from glossa.config import Settings
    from glossa.storage.base import StorageBackend


class SourceFetchError(RuntimeError):
    pass


async def _fetch_pull(source: Source) -> str:
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
    return resp.text


async def _fetch_upload(source: Source, storage: "StorageBackend", settings: "Settings") -> str:
    if not source.asset_path:
        raise SourceFetchError(f"upload-mode source {source.id} has no asset_path")
    try:
        data = await storage.read_asset(source.space_id, source.asset_path)
    except FileNotFoundError as e:
        raise SourceFetchError(f"uploaded asset missing for source {source.id}: {e}") from e
    filename = str(source.metadata.get("filename") or os.path.basename(source.asset_path))
    try:
        return await doc_parser.parse_asset_to_text(data=data, filename=filename, settings=settings)
    except doc_parser.DocumentParseError as e:
        raise SourceFetchError(str(e)) from e


async def fetch_content(
    source: Source,
    max_chars: int,
    *,
    storage: "StorageBackend",
    settings: "Settings",
) -> str:
    if source.ingestion_mode == SourceIngestionMode.PUSH:
        content = source.content_inline or ""
    elif source.ingestion_mode == SourceIngestionMode.PULL:
        content = await _fetch_pull(source)
    elif source.ingestion_mode == SourceIngestionMode.URL:
        if not source.external_uri:
            raise SourceFetchError(f"url-mode source {source.id} has no external_uri")
        try:
            content = await url_fetcher.fetch_url_as_markdown(source.external_uri, settings=settings)
        except url_fetcher.UrlFetchError as e:
            raise SourceFetchError(str(e)) from e
    elif source.ingestion_mode == SourceIngestionMode.UPLOAD:
        content = await _fetch_upload(source, storage, settings)
    else:  # pragma: no cover - defensive; StrEnum is exhaustive above
        raise SourceFetchError(f"unsupported ingestion_mode: {source.ingestion_mode}")

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
    return content
