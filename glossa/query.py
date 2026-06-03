"""Query a Space's wiki and return a synthesized answer with citations.

Two LLM calls: one to pick which pages to load from the index, one to
compose the answer from those pages. The wiki's index.md is the search
surface — at the 100s-of-pages scale the original LLM Wiki pattern targets
this works without a separate retrieval index.
"""

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from glossa.db.client import get_db
from glossa.ingest.prompts import (
    SYSTEM_QUERY_ANSWER,
    SYSTEM_QUERY_ROUTE,
    query_answer_user_prompt,
    query_route_user_prompt,
)
from glossa.llm import build_model, model_settings_for, resolve_model_name, resolve_provider, usage_to_dict
from glossa.models.source import Source
from glossa.usage import Operation, record_usage
from glossa.utils.wikilinks import extract_wikilinks, normalize_page_path

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from glossa.config import Settings
    from glossa.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class RouteOut(BaseModel):
    pages_to_load: list[str] = []
    reasoning: str | None = None


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


# Module-level agents — no model bound; model injected at run time.
query_route_agent = Agent(output_type=RouteOut, instructions=SYSTEM_QUERY_ROUTE)
query_answer_agent = Agent(output_type=str, instructions=SYSTEM_QUERY_ANSWER)


async def answer_question(
    *,
    space_id: str,
    request: QueryRequest,
    storage: "StorageBackend",
    settings: "Settings",
    model: "Model | None" = None,
) -> QueryResponse:
    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id})
    if not space_doc:
        raise RuntimeError(f"space {space_id} not found")
    from glossa.models.space import Space

    space = Space.model_validate(space_doc)

    if model is None:
        model = build_model(space, settings)

    effective_model = resolve_model_name(space, settings)
    provider = resolve_provider(space, settings)

    schema_markdown = await storage.read_page(space_id, "schema.md") or ""
    index_markdown = await storage.read_page(space_id, "index.md") or "(empty)"

    route_result = await query_route_agent.run(
        query_route_user_prompt(index_markdown=index_markdown, question=request.question, max_pages=request.max_pages),
        model=model,
        model_settings=model_settings_for(space, settings, temperature=0.0),
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.QUERY_ROUTE,
        model=effective_model,
        usage=usage_to_dict(route_result.usage, provider=provider),
    )
    route_out: RouteOut = route_result.output
    pages_to_load = [normalize_page_path(p) for p in route_out.pages_to_load][: request.max_pages]
    reasoning = route_out.reasoning

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

    answer_result = await query_answer_agent.run(
        query_answer_user_prompt(
            schema_markdown=schema_markdown,
            pages=pages_loaded,
            question=request.question,
        ),
        model=model,
        model_settings=model_settings_for(space, settings, temperature=0.2),
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.QUERY_ANSWER,
        model=effective_model,
        usage=usage_to_dict(answer_result.usage, provider=provider),
    )
    answer = answer_result.output.strip()

    cited_pages = sorted(set(extract_wikilinks(answer)))
    cited_sources = await _resolve_cited_sources(space_id, cited_pages)

    return QueryResponse(
        answer=answer,
        pages_consulted=[p["path"] for p in pages_loaded],
        cited_pages=cited_pages,
        cited_sources=cited_sources,
        reasoning=reasoning,
    )


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
