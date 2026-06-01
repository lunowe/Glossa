"""Read/merge/write one page during ingest.

Storage path convention: a page with logical path ``entities/companies/allianz``
is stored as the file ``pages/entities/companies/allianz.md`` in the space
bucket. Special pages (schema, index, log) live at the bucket root.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from glossa.db.client import get_db
from glossa.models.page import Page, PageKind
from glossa.usage.quota import check_storage_quota_before_write
from glossa.utils import frontmatter

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


def _storage_path(page_path: str) -> str:
    return f"pages/{page_path}.md"


async def read_existing_page(
    storage: "StorageBackend",
    space_id: str,
    page_path: str,
) -> str | None:
    content = await storage.read_page(space_id, _storage_path(page_path))
    return content or None


async def upsert_page(
    *,
    storage: "StorageBackend",
    space_id: str,
    page_path: str,
    kind: PageKind,
    title: str,
    new_content: str,
    source_refs: list[str],
    job_id: str,
    tenant_id: str | None = None,
) -> tuple[bool, bool]:
    """Write a page to storage and upsert its DB record.

    Returns ``(is_new, is_changed)``.

    If ``tenant_id`` is provided, the tenant's storage-bytes quota is
    enforced before the write. ``QuotaExceededError`` is raised on block;
    the ingest workflow translates that into a failed Job.
    """
    storage_path = _storage_path(page_path)
    existing = await storage.read_page(space_id, storage_path)
    is_new = not existing
    is_changed = existing != new_content
    if not is_changed:
        return is_new, is_changed

    size_bytes = len(new_content.encode("utf-8"))
    if tenant_id is not None:
        await check_storage_quota_before_write(tenant_id, size_bytes)

    await storage.write_page(space_id, storage_path, new_content)

    fm, _ = frontmatter.parse(new_content)
    db = get_db()
    now = datetime.now(UTC)
    await db.pages.update_one(
        {"space_id": space_id, "path": page_path},
        {
            "$set": Page(
                space_id=space_id,
                path=page_path,
                kind=kind,
                title=title,
                frontmatter=fm,
                source_refs=source_refs,
                backlinks=[],
                size_bytes=size_bytes,
                updated_at=now,
                last_touched_by_job_id=job_id,
            ).model_dump(),
        },
        upsert=True,
    )
    return is_new, is_changed


def build_summary_page(
    *,
    source_id: str,
    source_title: str,
    source_external_uri: str | None,
    source_metadata: dict,
    summary_markdown: str,
    entity_page_paths: list[str],
) -> str:
    """Build the markdown content for a summary page (deterministic — no LLM)."""
    now = datetime.now(UTC).isoformat()
    fm = {
        "kind": "summary",
        "title": source_title,
        "source_id": source_id,
        "external_uri": source_external_uri,
        "metadata": source_metadata,
        "entities": entity_page_paths,
        "updated_at": now,
    }
    entity_list = "\n".join(f"- [[{p}]]" for p in entity_page_paths) if entity_page_paths else "- *(none)*"
    external_block = f"\n\n**Quelle:** <{source_external_uri}>" if source_external_uri else ""
    body = f"""# {source_title}{external_block}

## Zusammenfassung

{summary_markdown}

## Erwähnte Entitäten

{entity_list}
"""
    return frontmatter.serialize(fm, body)
