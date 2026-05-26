"""Render ``lint_report.md`` and append the lint entry to ``log.md``.

Both files live at the space's bucket root, mirroring ``index.md`` and
``log.md``. The report is deterministic given the findings list — the same
findings produce byte-identical markdown.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from glossa.utils import frontmatter

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


REPORT_PATH = "lint_report.md"

_CATEGORY_LABELS = {
    "orphan": "Orphan pages",
    "broken_link": "Broken wikilinks",
    "contradiction": "Contradictions",
    "supersession": "Supersessions",
}


def _wikilink(path: str) -> str:
    return f"[[{path}]]"


def _render(findings: list[dict], *, pages_scanned: int, pages_with_llm_check: int, job_id: str) -> str:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    by_category: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_category[f["category"]].append(f)

    fm = {
        "kind": "system",
        "title": "Lint Report",
        "generated_at": now,
        "job_id": job_id,
        "pages_scanned": pages_scanned,
        "pages_with_llm_check": pages_with_llm_check,
        "findings_count": len(findings),
    }

    lines: list[str] = ["# Lint Report", "", f"Generated at {now} by job `{job_id}`.", ""]
    if pages_scanned == 0:
        lines.append("*(no pages in this space yet)*")
        return frontmatter.serialize(fm, "\n".join(lines).rstrip() + "\n")

    lines.append(f"Scanned **{pages_scanned}** pages, ran the contradiction check on **{pages_with_llm_check}**.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    if not findings:
        lines.append("- All checks clean. No findings.")
    else:
        for category in ("orphan", "broken_link", "contradiction", "supersession"):
            count = len(by_category.get(category, []))
            if count:
                lines.append(f"- {_CATEGORY_LABELS[category]}: **{count}**")
    lines.append("")

    if findings:
        for category in ("orphan", "broken_link", "contradiction", "supersession"):
            bucket = by_category.get(category) or []
            if not bucket:
                continue
            lines.append(f"## {_CATEGORY_LABELS[category]}")
            lines.append("")
            lines.extend(_render_bucket(category, bucket))
            lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    return frontmatter.serialize(fm, body)


def _render_bucket(category: str, findings: list[dict]) -> list[str]:
    lines: list[str] = []
    if category == "orphan":
        for f in findings:
            lines.append(f"- {_wikilink(f['page_path'])} — {f.get('detail') or 'no inbound wikilinks'}")
        return lines

    if category == "broken_link":
        for f in findings:
            target = (f.get("related_paths") or [""])[0]
            target_block = f" → `{target}`" if target else ""
            lines.append(f"- {_wikilink(f['page_path'])}{target_block} — {f.get('detail') or 'target missing'}")
        return lines

    for f in findings:
        lines.append(f"### {_wikilink(f['page_path'])}")
        lines.append("")
        claim = f.get("claim") or f.get("detail") or "(no claim)"
        lines.append(f"- **Claim:** {claim}")
        if f.get("explanation"):
            lines.append(f"- **Why:** {f['explanation']}")
        related = f.get("related_paths") or []
        if related:
            lines.append("- **Sources:** " + ", ".join(_wikilink(r) for r in related))
        lines.append("")
    return lines


async def write_report(
    *,
    storage: "StorageBackend",
    space_id: str,
    findings: list[dict],
    pages_scanned: int,
    pages_with_llm_check: int,
    job_id: str,
) -> str:
    content = _render(
        findings,
        pages_scanned=pages_scanned,
        pages_with_llm_check=pages_with_llm_check,
        job_id=job_id,
    )
    await storage.write_page(space_id, REPORT_PATH, content)
    return content


async def append_lint_log_entry(
    *,
    storage: "StorageBackend",
    space_id: str,
    summary: dict[str, int],
    job_id: str,
) -> None:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    total = sum(summary.values())
    if total == 0:
        headline = "no findings"
    else:
        parts = [f"{count} {_CATEGORY_LABELS.get(k, k).lower()}" for k, count in summary.items() if count]
        headline = ", ".join(parts)

    lines = [
        f"## [{timestamp}] lint | {headline}",
        f"- Job: `{job_id}`",
        f"- Report: {_wikilink('lint_report')}",
    ]
    entry = "\n".join(lines) + "\n\n"

    existing = await storage.read_page(space_id, "log.md")
    if not existing:
        existing = "# Log\n\n"
    await storage.write_page(space_id, "log.md", existing + entry)
