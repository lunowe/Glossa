"""Deterministic regeneration of index.md from DB page records."""

from collections import defaultdict
from typing import TYPE_CHECKING

from glossa.db.client import get_db

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


async def regenerate_index(*, storage: "StorageBackend", space_id: str) -> str:
    db = get_db()
    cursor = db.pages.find({"space_id": space_id}, {"path": 1, "kind": 1, "title": 1, "source_refs": 1})

    by_group: dict[str, list[dict]] = defaultdict(list)
    async for doc in cursor:
        path = doc["path"]
        if path.startswith("entities/"):
            group_segments = path.split("/", 2)
            group = group_segments[1] if len(group_segments) > 1 else "other"
            by_group[f"entities/{group}"].append(doc)
        elif path.startswith("summaries/"):
            by_group["summaries"].append(doc)
        elif path.startswith("syntheses/"):
            by_group["syntheses"].append(doc)
        elif path.startswith("notes/"):
            by_group["notes"].append(doc)
        else:
            by_group["other"].append(doc)

    lines: list[str] = ["# Index", ""]
    if not by_group:
        lines.append("*(no pages yet)*")
        content = "\n".join(lines) + "\n"
        await storage.write_page(space_id, "index.md", content)
        return content

    for group in sorted(by_group):
        heading = group.replace("entities/", "Entities — ").replace("_", " ").title()
        lines.append(f"## {heading}")
        lines.append("")
        for doc in sorted(by_group[group], key=lambda d: d.get("title", "")):
            n_sources = len(doc.get("source_refs") or [])
            suffix = f" — {n_sources} source(s)" if n_sources else ""
            lines.append(f"- [[{doc['path']}]] — {doc.get('title', '?')}{suffix}")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    await storage.write_page(space_id, "index.md", content)
    return content
