"""Append entries to log.md.

Entries use a consistent prefix so the log is parseable with simple tools:

    ## [2026-04-02T14:23:00Z] ingest | Article Title
    - Created: [[entities/companies/allianz]]
    - Updated: [[entities/topics/cyberversicherung]]
    - Summary: [[summaries/src-abc123]]
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


async def append_log_entry(
    *,
    storage: "StorageBackend",
    space_id: str,
    kind: str,
    title: str,
    pages_created: list[str],
    pages_updated: list[str],
    summary_path: str | None,
    note: str | None = None,
) -> None:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [f"## [{timestamp}] {kind} | {title}"]
    if pages_created:
        lines.append("- Created: " + ", ".join(f"[[{p}]]" for p in pages_created))
    if pages_updated:
        lines.append("- Updated: " + ", ".join(f"[[{p}]]" for p in pages_updated))
    if summary_path:
        lines.append(f"- Summary: [[{summary_path}]]")
    if note:
        lines.append(f"- Note: {note}")
    entry = "\n".join(lines) + "\n\n"

    existing = await storage.read_page(space_id, "log.md")
    if not existing:
        existing = "# Log\n\n"
    await storage.write_page(space_id, "log.md", existing + entry)
