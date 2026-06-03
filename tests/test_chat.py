import json
from datetime import UTC, datetime

from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from glossa.chat import ChatMessage, ChatRequest, answer_chat, chat_agent, chat_event_stream
from glossa.db.client import get_db
from glossa.models.page import Page, PageKind
from glossa.models.source import Source, SourceIngestionMode
from glossa.models.space import Space


async def _seed_chat_wiki(storage):
    db = get_db()
    now = datetime.now(UTC)
    space = Space(
        id="gls_chat",
        tenant_id="t1",
        name="Chat Space",
        slug="chat-space",
        bucket_uri="mem://gls_chat/",
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space(space.id)

    source = Source(
        id="src_a",
        space_id=space.id,
        title="Allianz note",
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline="Allianz offers cyber coverage.",
        external_uri="https://example.com/a",
        created_at=now,
    )
    await db.sources.insert_one(source.model_dump())

    page = Page(
        space_id=space.id,
        path="entities/company/allianz",
        kind=PageKind.ENTITY,
        title="Allianz",
        source_refs=["src_a"],
        updated_at=now,
    )
    await db.pages.insert_one(page.model_dump())
    await storage.write_page(
        space.id,
        "pages/entities/company/allianz.md",
        "# Allianz\n\nAllianz offers cyber coverage ([[summaries/src-src_a]]).",
    )
    await storage.write_page(
        space.id,
        "index.md",
        "# Index\n\n## Entities - Company\n\n- [[entities/company/allianz]] - Allianz - 1 source\n",
    )
    return space


def _chat_model(tool_calls, *, answer: str):
    def fn(messages, info):
        tool_returns = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart | RetryPromptPart)
        ]
        if len(tool_returns) < len(tool_calls):
            name, args = tool_calls[len(tool_returns)]
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart(content=answer)])

    return FunctionModel(fn)


def _chat_stream_model(tool_calls, *, answer: str):
    def fn(messages, info):
        tool_returns = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart | RetryPromptPart)
        ]
        if len(tool_returns) < len(tool_calls):
            name, args = tool_calls[len(tool_returns)]
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart(content=answer)])

    async def stream(messages, info):
        tool_returns = [
            p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart | RetryPromptPart)
        ]
        if len(tool_returns) < len(tool_calls):
            name, args = tool_calls[len(tool_returns)]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call-{name}")}
            return
        yield answer

    return FunctionModel(fn, stream_function=stream)


async def test_chat_reads_pages_and_returns_citations(storage, settings):
    space = await _seed_chat_wiki(storage)
    model = _chat_model(
        [
            ("read_index", {}),
            ("read_page", {"path": "entities/company/allianz"}),
        ],
        answer="Allianz offers cyber coverage ([[entities/company/allianz]]).",
    )

    with chat_agent.override(model=model):
        response = await answer_chat(
            space_id=space.id,
            request=ChatRequest(messages=[ChatMessage(content="What does Allianz offer?")]),
            storage=storage,
            settings=settings,
        )

    assert response.pages_consulted == ["entities/company/allianz"]
    assert response.cited_pages == ["entities/company/allianz"]
    assert response.cited_sources[0].id == "src_a"
    assert [event.name for event in response.tool_calls] == ["read_index", "read_page"]


async def test_chat_save_note_requires_allow_writes(storage, settings):
    space = await _seed_chat_wiki(storage)
    model = _chat_model(
        [
            (
                "save_note",
                {
                    "title": "Allianz cyber takeaway",
                    "body": "Allianz coverage matters ([[entities/company/allianz]]).",
                },
            )
        ],
        answer="I cannot save unless writes are enabled.",
    )

    with chat_agent.override(model=model):
        response = await answer_chat(
            space_id=space.id,
            request=ChatRequest(messages=[ChatMessage(content="Save this note")], allow_writes=False),
            storage=storage,
            settings=settings,
        )

    assert response.saved_pages == []
    assert not await storage.read_page(space.id, "pages/notes/allianz-cyber-takeaway.md")


async def test_chat_can_save_note_when_allowed(storage, settings):
    space = await _seed_chat_wiki(storage)
    model = _chat_model(
        [
            (
                "save_note",
                {
                    "title": "Allianz cyber takeaway",
                    "body": "Allianz coverage matters ([[entities/company/allianz]]).",
                },
            )
        ],
        answer="Saved [[notes/allianz-cyber-takeaway]].",
    )

    with chat_agent.override(model=model):
        response = await answer_chat(
            space_id=space.id,
            request=ChatRequest(messages=[ChatMessage(content="Save this note")], allow_writes=True),
            storage=storage,
            settings=settings,
        )

    assert response.saved_pages == ["notes/allianz-cyber-takeaway"]
    note = await storage.read_page(space.id, "pages/notes/allianz-cyber-takeaway.md")
    assert "kind: custom" in note
    assert "[[entities/company/allianz]]" in note
    index = await storage.read_page(space.id, "index.md")
    assert "## Notes" in index
    assert "[[notes/allianz-cyber-takeaway]]" in index
    assert "chat | Allianz cyber takeaway" in await storage.read_page(space.id, "log.md")


async def test_chat_stream_emits_tool_and_final_events(storage, settings):
    space = await _seed_chat_wiki(storage)
    model = _chat_stream_model(
        [("read_index", {})],
        answer="Allianz is in the wiki ([[entities/company/allianz]]).",
    )

    with chat_agent.override(model=model):
        chunks = [
            chunk
            async for chunk in chat_event_stream(
                space_id=space.id,
                request=ChatRequest(messages=[ChatMessage(content="Allianz?")]),
                storage=storage,
                settings=settings,
            )
        ]

    rendered = "".join(chunks)
    assert "event: tool_call" in rendered
    assert '"name": "read_index"' in rendered
    assert "event: final" in rendered
