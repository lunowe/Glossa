"""Interactive chat over a Space's wiki.

This is deliberately wiki-native: chat can search/read existing pages and, when
explicitly allowed, save a compact note back into the wiki. Saved notes regenerate
``index.md`` and append ``log.md`` entries, so useful conversations compound
without introducing a second knowledge store.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent, PartDeltaEvent, TextPartDelta
from starlette.responses import StreamingResponse

from glossa.concurrency import lock_for_space
from glossa.db.client import get_db
from glossa.ingest import index_writer, log_writer, page_writer
from glossa.llm import build_model, model_settings_for, resolve_model_name, resolve_provider, usage_to_dict
from glossa.models.page import PageKind
from glossa.models.space import Space
from glossa.query import CitedSource, _resolve_cited_sources
from glossa.usage import Operation, record_usage
from glossa.utils import frontmatter
from glossa.utils.slug import slugify
from glossa.utils.wikilinks import extract_wikilinks, normalize_page_path

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from glossa.config import Settings
    from glossa.storage.base import StorageBackend


SYSTEM_CHAT = """You are Glossa's interactive wiki chat agent.

Use the wiki as your working memory. Read `index.md` first when the answer
depends on stored knowledge, then search/read only the pages needed. Cite wiki
pages with `[[path]]` wikilinks. If the wiki lacks enough evidence, say what is
missing instead of guessing.

You may receive posted context from the user. You can discuss it, compare it to
the wiki, or suggest ingestion. Posted context is not a durable source unless the
user explicitly asks to save something.

When writes are allowed, save a note only if the user asks to save, remember,
file, or persist the result. Saved notes must be compact, useful later, and
cited with existing wiki links where possible. Do not create entity/topic pages
from chat; ingest remains responsible for canonical page maintenance.
"""


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    context: str | None = None
    max_pages: int = Field(default=8, ge=1, le=20)
    allow_writes: bool = False


class ChatToolEvent(BaseModel):
    name: str
    args: dict | str | None = None
    result: str | None = None


class ChatResponse(BaseModel):
    answer: str
    pages_consulted: list[str]
    cited_pages: list[str]
    cited_sources: list[CitedSource]
    saved_pages: list[str] = Field(default_factory=list)
    tool_calls: list[ChatToolEvent] = Field(default_factory=list)


@dataclass
class ChatDeps:
    storage: "StorageBackend"
    space: Space
    request: ChatRequest
    pages_consulted: list[str] = field(default_factory=list)
    saved_pages: list[str] = field(default_factory=list)
    tool_events: list[ChatToolEvent] = field(default_factory=list)


chat_agent = Agent(deps_type=ChatDeps, output_type=str, instructions=SYSTEM_CHAT)


def _prompt_from_request(request: ChatRequest) -> str:
    lines: list[str] = []
    if request.context and request.context.strip():
        lines.extend(["Posted context:", request.context.strip(), ""])
    lines.append("Conversation:")
    if request.messages:
        for message in request.messages:
            lines.append(f"{message.role}: {message.content}")
    else:
        lines.append("user: (empty)")
    lines.append("")
    lines.append(f"Page read budget: at most {request.max_pages} pages.")
    if request.allow_writes:
        lines.append("Writes are allowed only for durable user-requested notes under notes/<slug>.")
    else:
        lines.append("Writes are not allowed in this request.")
    return "\n".join(lines)


def _record_tool(ctx: RunContext[ChatDeps], event: ChatToolEvent) -> None:
    ctx.deps.tool_events.append(event)


def _score_page(*, query: str, path: str, title: str) -> int:
    needle = query.strip().lower()
    terms = [term for term in slugify(query).split("-") if len(term) >= 3]
    haystack = f"{path.lower()} {title.lower()} {slugify(title)}"
    score = 100 if needle and needle in haystack else 0
    score += sum(1 for term in terms if term in haystack)
    return score


async def _known_paths(space_id: str, *, extra: list[str] | None = None) -> set[str]:
    db = get_db()
    paths = {normalize_page_path(path) for path in extra or [] if normalize_page_path(path)}
    cursor = db.pages.find({"space_id": space_id}, {"path": 1})
    async for doc in cursor:
        paths.add(normalize_page_path(doc["path"]))
    return paths


async def _source_refs_for_links(space_id: str, cited_pages: list[str]) -> list[str]:
    db = get_db()
    refs: list[str] = []
    cursor = db.pages.find({"space_id": space_id, "path": {"$in": cited_pages}}, {"source_refs": 1})
    async for doc in cursor:
        refs.extend(doc.get("source_refs") or [])
    return list(dict.fromkeys(refs))


@chat_agent.tool
async def read_index(ctx: RunContext[ChatDeps]) -> str:
    """Read index.md, the compact catalog of wiki pages."""
    _record_tool(ctx, ChatToolEvent(name="read_index"))
    return await ctx.deps.storage.read_page(ctx.deps.space.id, "index.md") or "(empty)"


@chat_agent.tool
async def read_recent_log(ctx: RunContext[ChatDeps], tail: int = 10) -> str:
    """Read the last entries from log.md when recency/history matters."""
    tail = max(1, min(tail, 20))
    content = await ctx.deps.storage.read_page(ctx.deps.space.id, "log.md") or ""
    lines = content.splitlines()
    entry_indices = [i for i, line in enumerate(lines) if line.startswith("## [")]
    if entry_indices and len(entry_indices) > tail:
        content = "\n".join(lines[entry_indices[-tail] :])
    _record_tool(ctx, ChatToolEvent(name="read_recent_log", args={"tail": tail}))
    return content or "(empty)"


@chat_agent.tool
async def search_pages(ctx: RunContext[ChatDeps], query: str) -> list[dict]:
    """Search page metadata by title/path before reading specific pages."""
    db = get_db()
    cursor = db.pages.find({"space_id": ctx.deps.space.id}, {"path": 1, "title": 1, "kind": 1})
    scored: list[tuple[int, dict]] = []
    async for doc in cursor:
        title = doc.get("title") or ""
        score = _score_page(query=query, path=doc["path"], title=title)
        if score:
            scored.append((score, {"path": doc["path"], "title": title, "kind": doc.get("kind", "")}))
    results = [item for _score, item in sorted(scored, key=lambda row: (-row[0], row[1]["path"]))[:20]]
    _record_tool(ctx, ChatToolEvent(name="search_pages", args={"query": query}, result=f"{len(results)} result(s)"))
    return results


@chat_agent.tool
async def read_page(ctx: RunContext[ChatDeps], path: str) -> str:
    """Read one wiki page by logical path."""
    path = normalize_page_path(path)
    if not path:
        raise ModelRetry("Provide a non-empty logical page path.")
    if path not in ctx.deps.pages_consulted:
        if len(ctx.deps.pages_consulted) >= ctx.deps.request.max_pages:
            raise ModelRetry(f"Page budget reached ({ctx.deps.request.max_pages}). Answer with loaded pages.")
        ctx.deps.pages_consulted.append(path)
    content = await ctx.deps.storage.read_page(ctx.deps.space.id, f"pages/{path}.md")
    if not content:
        raise ModelRetry(f"Page {path!r} does not exist. Search or read the index before trying another path.")
    _record_tool(ctx, ChatToolEvent(name="read_page", args={"path": path}))
    return content


@chat_agent.tool
async def save_note(ctx: RunContext[ChatDeps], title: str, body: str) -> str:
    """Save a compact durable chat/query result under notes/<slug>.

    Use only when the user explicitly asked to save, file, remember, or persist
    the result. `body` should be markdown without frontmatter.
    """
    if not ctx.deps.request.allow_writes:
        raise ModelRetry("Writes are disabled for this chat request. Answer without saving.")
    title = title.strip()
    body = body.strip()
    if not title or not body:
        raise ModelRetry("Saved notes need a title and non-empty body.")
    path = f"notes/{slugify(title) or 'note'}"
    cited_pages = extract_wikilinks(body)
    known = await _known_paths(ctx.deps.space.id, extra=[path])
    missing = [target for target in cited_pages if "/" in target and target not in known]
    if missing:
        raise ModelRetry("The note contains missing wikilinks: " + ", ".join(f"[[{m}]]" for m in missing[:10]))

    source_refs = await _source_refs_for_links(ctx.deps.space.id, cited_pages)
    now = datetime.now(UTC).isoformat()
    content = frontmatter.serialize(
        {
            "kind": PageKind.CUSTOM.value,
            "title": title,
            "source_refs": source_refs,
            "updated_at": now,
        },
        f"# {title}\n\n{body}\n",
    )

    async with lock_for_space(ctx.deps.space.id):
        is_new, is_changed = await page_writer.upsert_page(
            storage=ctx.deps.storage,
            space_id=ctx.deps.space.id,
            page_path=path,
            kind=PageKind.CUSTOM,
            title=title,
            new_content=content,
            source_refs=source_refs,
            job_id="chat",
            tenant_id=ctx.deps.space.tenant_id,
        )
        if is_changed:
            await index_writer.regenerate_index(storage=ctx.deps.storage, space_id=ctx.deps.space.id)
            await log_writer.append_log_entry(
                storage=ctx.deps.storage,
                space_id=ctx.deps.space.id,
                kind="chat",
                title=title,
                pages_created=[path] if is_new else [],
                pages_updated=[] if is_new else [path],
                summary_path=None,
                note="saved chat note",
            )

    if path not in ctx.deps.saved_pages:
        ctx.deps.saved_pages.append(path)
    _record_tool(ctx, ChatToolEvent(name="save_note", args={"title": title}, result=path))
    return f"saved [[{path}]]"


async def answer_chat(
    *,
    space_id: str,
    request: ChatRequest,
    storage: "StorageBackend",
    settings: "Settings",
    model: "Model | None" = None,
) -> ChatResponse:
    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id})
    if not space_doc:
        raise RuntimeError(f"space {space_id} not found")
    space = Space.model_validate(space_doc)
    if model is None:
        model = build_model(space, settings)
    provider = resolve_provider(space, settings)
    effective_model = resolve_model_name(space, settings)
    deps = ChatDeps(storage=storage, space=space, request=request)

    result = await chat_agent.run(
        _prompt_from_request(request),
        model=model,
        model_settings=model_settings_for(space, settings, temperature=0.2),
        deps=deps,
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.CHAT,
        model=effective_model,
        usage=usage_to_dict(result.usage, provider=provider),
    )
    answer = result.output.strip()
    cited_pages = sorted(set(extract_wikilinks(answer)))
    return ChatResponse(
        answer=answer,
        pages_consulted=deps.pages_consulted,
        cited_pages=cited_pages,
        cited_sources=await _resolve_cited_sources(space_id, cited_pages),
        saved_pages=deps.saved_pages,
        tool_calls=deps.tool_events,
    )


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _preview(value, *, limit: int = 500):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit].rstrip() + "..."
    if isinstance(value, dict):
        return {k: _preview(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_preview(v, limit=limit) for v in value]
    return value


async def chat_event_stream(
    *,
    space_id: str,
    request: ChatRequest,
    storage: "StorageBackend",
    settings: "Settings",
    model: "Model | None" = None,
) -> AsyncIterator[str]:
    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id})
    if not space_doc:
        yield _sse("error", {"error": f"space {space_id} not found"})
        return
    space = Space.model_validate(space_doc)
    if model is None:
        model = build_model(space, settings)
    provider = resolve_provider(space, settings)
    effective_model = resolve_model_name(space, settings)
    deps = ChatDeps(storage=storage, space=space, request=request)
    answer_chunks: list[str] = []

    async with chat_agent.run_stream_events(
        _prompt_from_request(request),
        model=model,
        model_settings=model_settings_for(space, settings, temperature=0.2),
        deps=deps,
    ) as stream:
        async for event in stream:
            if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                chunk = event.delta.content_delta
                answer_chunks.append(chunk)
                yield _sse("delta", {"content": chunk})
            elif isinstance(event, FunctionToolCallEvent):
                yield _sse("tool_call", {"name": event.part.tool_name, "args": _preview(event.part.args)})
            elif isinstance(event, FunctionToolResultEvent):
                content = _preview(event.content) if isinstance(event.content, str) else None
                yield _sse("tool_result", {"name": event.part.tool_name, "result": content})
            elif getattr(event, "event_kind", "") == "agent_run_result":
                result = event.result
                await record_usage(
                    tenant_id=space.tenant_id,
                    space_id=space.id,
                    operation=Operation.CHAT,
                    model=effective_model,
                    usage=usage_to_dict(result.usage, provider=provider),
                )
                answer = "".join(answer_chunks) or str(result.output).strip()
                cited_pages = sorted(set(extract_wikilinks(answer)))
                response = ChatResponse(
                    answer=answer,
                    pages_consulted=deps.pages_consulted,
                    cited_pages=cited_pages,
                    cited_sources=await _resolve_cited_sources(space_id, cited_pages),
                    saved_pages=deps.saved_pages,
                    tool_calls=deps.tool_events,
                )
                yield _sse("final", response.model_dump())


def streaming_response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream")
