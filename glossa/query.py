"""Query a Space's wiki and return a synthesized answer with citations.

Two LLM calls: one to pick which pages to load from the index, one to
compose the answer from those pages. The wiki's index.md is the search
surface — at the 100s-of-pages scale the original LLM Wiki pattern targets
this works without a separate retrieval index.
"""

import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from glossa.db.client import get_db
from glossa.ingest.prompts import (
    SYSTEM_QUERY_ANSWER,
    SYSTEM_QUERY_ROUTE,
    query_answer_user_prompt,
    query_route_user_prompt,
)
from glossa.llm import build_driver
from glossa.llm.base import LLMDriver, LLMMessage
from glossa.models.source import Source
from glossa.models.space import LLMMode, Space
from glossa.usage import Operation, record_usage
from glossa.utils.json_parse import LLMJSONError, parse

if TYPE_CHECKING:
    from glossa.config import Settings
    from glossa.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class QueryRequest(BaseModel):
    question: str
    max_pages: int = Field(default=8, ge=1, le=20)


class CitedSource(BaseModel):
    id: str
    title: str
    external_uri: str | None = None


class QueryResponse(BaseModel):
    answer: str
    pages_consulted: list[str]
    cited_pages: list[str]
    cited_sources: list[CitedSource]
    reasoning: str | None = None


async def answer_question(
    *,
    space_id: str,
    request: QueryRequest,
    storage: "StorageBackend",
    settings: "Settings",
    llm: LLMDriver | None = None,
) -> QueryResponse:
    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id})
    if not space_doc:
        raise RuntimeError(f"space {space_id} not found")
    space = Space.model_validate(space_doc)

    if llm is None:
        llm = build_driver(space, settings)

    effective_model = _resolve_effective_model(space, settings)

    schema_markdown = await storage.read_page(space_id, "schema.md") or ""
    index_markdown = await storage.read_page(space_id, "index.md") or "(empty)"

    route_response = await llm.chat(
        [
            LLMMessage(role="system", content=SYSTEM_QUERY_ROUTE),
            LLMMessage(
                role="user", content=query_route_user_prompt(index_markdown=index_markdown, question=request.question)
            ),
        ],
        temperature=0.0,
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.QUERY_ROUTE,
        model=effective_model,
        usage=dict(route_response.usage or {}),
    )
    route_data = parse(route_response.content)
    if not isinstance(route_data, dict):
        raise LLMJSONError("query routing expected a JSON object")
    pages_to_load = [str(p).removesuffix(".md") for p in route_data.get("pages_to_load") or []][: request.max_pages]
    reasoning = route_data.get("reasoning")

    pages_loaded: list[dict] = []
    for path in pages_to_load:
        content = await storage.read_page(space_id, f"pages/{path}.md")
        if content:
            pages_loaded.append({"path": path, "content": content})

    if not pages_loaded:
        return QueryResponse(
            answer="Die Wissensbasis enthält keine passenden Seiten für diese Frage.",
            pages_consulted=[],
            cited_pages=[],
            cited_sources=[],
            reasoning=reasoning,
        )

    answer_response = await llm.chat(
        [
            LLMMessage(role="system", content=SYSTEM_QUERY_ANSWER),
            LLMMessage(
                role="user",
                content=query_answer_user_prompt(
                    schema_markdown=schema_markdown,
                    pages=pages_loaded,
                    question=request.question,
                ),
            ),
        ],
        temperature=0.2,
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.QUERY_ANSWER,
        model=effective_model,
        usage=dict(answer_response.usage or {}),
    )
    answer = answer_response.content.strip()

    cited_pages = sorted({m.group(1).removesuffix(".md") for m in _WIKILINK_RE.finditer(answer)})
    cited_sources = await _resolve_cited_sources(space_id, cited_pages)

    return QueryResponse(
        answer=answer,
        pages_consulted=[p["path"] for p in pages_loaded],
        cited_pages=cited_pages,
        cited_sources=cited_sources,
        reasoning=reasoning,
    )


def _resolve_effective_model(space: Space, settings: "Settings") -> str:
    cfg = space.llm_config
    if cfg.model:
        return cfg.model
    if cfg.mode == LLMMode.HOSTED:
        return settings.hosted_default_model
    return settings.default_llm_model


async def _resolve_cited_sources(space_id: str, cited_pages: list[str]) -> list[CitedSource]:
    if not cited_pages:
        return []
    db = get_db()
    source_ids: set[str] = set()
    cursor = db.pages.find(
        {"space_id": space_id, "path": {"$in": cited_pages}},
        {"source_refs": 1},
    )
    async for doc in cursor:
        for sid in doc.get("source_refs") or []:
            source_ids.add(sid)
    if not source_ids:
        return []
    cursor = db.sources.find({"space_id": space_id, "id": {"$in": list(source_ids)}})
    return [
        CitedSource(
            id=src.id,
            title=src.title,
            external_uri=src.external_uri,
        )
        async for src in (Source.model_validate(doc) async for doc in cursor)
    ]
