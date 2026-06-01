"""LLM-driven contradiction / supersession detection.

For each page that cites at least 2 sources, fetch the source summaries and
ask the LLM whether any claims on the page are contradicted or superseded.
Pages with 0 or 1 cited sources are skipped — they cannot contradict themselves.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel
from pydantic_ai import Agent

from glossa.db.client import get_db
from glossa.lint.prompts import SYSTEM_LINT_CONTRADICTIONS, contradictions_user_prompt
from glossa.lint.scanner import PageRecord
from glossa.llm import usage_to_dict

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from glossa.storage.base import StorageBackend


class FindingOut(BaseModel):
    claim: str
    kind: Literal["contradiction", "supersession"] = "contradiction"
    explanation: str = ""
    source_ids: list[str] = []


class ContradictionsOut(BaseModel):
    findings: list[FindingOut] = []


@dataclass
class ContradictionFinding:
    page_path: str
    claim: str
    kind: str
    explanation: str
    source_ids: list[str]
    related_paths: list[str]


# Module-level agent — no model bound; model injected at run time.
contradictions_agent = Agent(output_type=ContradictionsOut, instructions=SYSTEM_LINT_CONTRADICTIONS)


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
    model: "Model",
    provider: str,
    storage: "StorageBackend",
    space_id: str,
    schema_markdown: str,
    page: PageRecord,
) -> tuple[list[ContradictionFinding], dict | None]:
    """Run the contradiction check on one page.

    Returns ``(findings, usage)``. ``usage`` is ``None`` when no LLM call was
    made (page had <2 source citations); otherwise it is the provider usage
    dict, suitable for ``record_usage``.
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
    result = await contradictions_agent.run(
        user_prompt,
        model=model,
        model_settings={"temperature": 0.1},
    )
    usage = usage_to_dict(result.usage, provider=provider)
    out: ContradictionsOut = result.output

    findings: list[ContradictionFinding] = []
    for f in out.findings:
        claim = f.claim.strip()
        if not claim:
            continue
        kind = f.kind
        if kind not in ("contradiction", "supersession"):
            kind = "contradiction"
        source_ids = [str(s) for s in f.source_ids if s]
        related = [f"summaries/src-{sid}" for sid in source_ids]
        findings.append(
            ContradictionFinding(
                page_path=page.path,
                claim=claim,
                kind=kind,
                explanation=f.explanation.strip(),
                source_ids=source_ids,
                related_paths=related,
            )
        )
    return findings, usage
