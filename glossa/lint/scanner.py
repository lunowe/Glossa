"""Deterministic scans over a space's pages.

The scanner produces two finding categories with no LLM calls:

- **orphan** — a page that no other page wikilinks to
- **broken_link** — a ``[[path]]`` whose target page does not exist

System pages (``index``, ``log``, ``schema``, ``lint_report``) are treated as
valid targets but never as orphans (they are auto-maintained and outside the
``pages/`` collection).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from glossa.db.client import get_db
from glossa.utils.wikilinks import extract_wikilinks

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


SYSTEM_PAGE_TARGETS = frozenset({"index", "log", "schema", "lint_report"})


@dataclass
class PageRecord:
    path: str
    title: str
    kind: str
    source_refs: list[str] = field(default_factory=list)
    content: str = ""


@dataclass
class ScanFinding:
    category: str
    page_path: str
    detail: str
    related_paths: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    findings: list[ScanFinding]
    pages: list[PageRecord]


def _storage_path(page_path: str) -> str:
    return f"pages/{page_path}.md"


async def load_pages(storage: "StorageBackend", space_id: str) -> list[PageRecord]:
    """Read every page record + its current storage content for the space."""
    db = get_db()
    cursor = db.pages.find(
        {"space_id": space_id},
        {"path": 1, "title": 1, "kind": 1, "source_refs": 1},
    )
    records: list[PageRecord] = []
    async for doc in cursor:
        path = doc["path"]
        content = await storage.read_page(space_id, _storage_path(path))
        records.append(
            PageRecord(
                path=path,
                title=doc.get("title") or path,
                kind=doc.get("kind") or "custom",
                source_refs=list(doc.get("source_refs") or []),
                content=content or "",
            )
        )
    return records


def scan_deterministic(pages: list[PageRecord]) -> ScanResult:
    """Run all deterministic checks over an already-loaded page set.

    Pure function over its inputs — useful for unit tests.
    """
    known_paths = {p.path for p in pages}
    inbound: dict[str, set[str]] = {p.path: set() for p in pages}
    findings: list[ScanFinding] = []

    for page in pages:
        links = extract_wikilinks(page.content)
        for target in links:
            if target in SYSTEM_PAGE_TARGETS:
                continue
            if target == page.path:
                continue
            if target in known_paths:
                inbound[target].add(page.path)
                continue
            findings.append(
                ScanFinding(
                    category="broken_link",
                    page_path=page.path,
                    detail=f"links to missing page [[{target}]]",
                    related_paths=[target],
                )
            )

    for page in pages:
        if not inbound[page.path]:
            findings.append(
                ScanFinding(
                    category="orphan",
                    page_path=page.path,
                    detail=f"no inbound wikilinks from other pages ({page.kind})",
                    related_paths=[],
                )
            )

    return ScanResult(findings=findings, pages=pages)
