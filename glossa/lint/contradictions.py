"""LLM-driven contradiction / supersession detection.

For each page that cites at least 2 sources, fetch the source summaries and
ask the LLM whether any claims on the page are contradicted or superseded.
Pages with 0 or 1 cited sources are skipped — they cannot contradict themselves.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from glossa.db.client import get_db
from glossa.lint.prompts import SYSTEM_LINT_CONTRADICTIONS, contradictions_user_prompt
from glossa.lint.scanner import PageRecord
from glossa.llm.base import LLMDriver, LLMMessage
from glossa.utils.json_parse import LLMJSONError, parse

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


@dataclass
class ContradictionFinding:
    page_path: str
    claim: str
    kind: str
    explanation: str
    source_ids: list[str]
    related_paths: list[str]


def _summary_storage_path(source_id: str) -> str:
    return f"pages/summaries/src-{source_id}.md"


async def _load_source_summaries(
    *,
    storage: "StorageBackend",
    space_id: str,
    source_ids: list[str],
) -> list[dict]:
    db = get_db()
    summaries: list[dict] = []
    for source_id in source_ids:
        content = await storage.read_page(space_id, _summary_storage_path(source_id))
        if not content:
            continue
        src_doc = await db.sources.find_one(
            {"id": source_id, "space_id": space_id},
            {"title": 1, "created_at": 1},
        )
        summaries.append(
            {
                "source_id": source_id,
                "title": (src_doc or {}).get("title"),
                "created_at": (src_doc or {}).get("created_at"),
                "summary": content,
            }
        )
    return summaries


async def check_page_for_contradictions(
    *,
    llm: LLMDriver,
    storage: "StorageBackend",
    space_id: str,
    schema_markdown: str,
    page: PageRecord,
) -> tuple[list[ContradictionFinding], dict | None]:
    """Run the contradiction check on one page.

    Returns ``(findings, usage)``. ``usage`` is ``None`` when no LLM call was
    made (page had <2 source citations); otherwise it is the raw provider
    usage dict, suitable for ``record_usage``.
    """
    summaries = await _load_source_summaries(
        storage=storage,
        space_id=space_id,
        source_ids=page.source_refs,
    )
    if len(summaries) < 2:
        return [], None

    user_prompt = contradictions_user_prompt(
        schema_markdown=schema_markdown,
        page_path=page.path,
        page_content=page.content,
        source_summaries=summaries,
    )
    response = await llm.chat(
        [
            LLMMessage(role="system", content=SYSTEM_LINT_CONTRADICTIONS),
            LLMMessage(role="user", content=user_prompt),
        ],
        temperature=0.1,
    )
    usage = dict(response.usage or {})
    data = parse(response.content)
    if not isinstance(data, dict):
        raise LLMJSONError("contradictions step expected a JSON object")
    raw_findings = data.get("findings") or []

    findings: list[ContradictionFinding] = []
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        claim = str(f.get("claim") or "").strip()
        if not claim:
            continue
        kind = f.get("kind") or "contradiction"
        if kind not in ("contradiction", "supersession"):
            kind = "contradiction"
        source_ids = [str(s) for s in (f.get("source_ids") or []) if s]
        related = [f"summaries/src-{sid}" for sid in source_ids]
        findings.append(
            ContradictionFinding(
                page_path=page.path,
                claim=claim,
                kind=kind,
                explanation=str(f.get("explanation") or "").strip(),
                source_ids=source_ids,
                related_paths=related,
            )
        )
    return findings, usage
