"""End-to-end query test using a fake LLM and in-memory storage."""

import json
from datetime import UTC, datetime

from glossa.db.client import get_db
from glossa.models.page import Page, PageKind
from glossa.models.source import Source, SourceIngestionMode
from glossa.models.space import Space
from glossa.query import QueryRequest, answer_question
from tests.fake_llm import FakeLLMDriver


async def _seed_wiki(storage):
    db = get_db()
    now = datetime.now(UTC)
    space = Space(
        id="gls_q",
        tenant_id="t1",
        name="Q Space",
        slug="q-space",
        bucket_uri="mem://gls_q/",
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space("gls_q")

    src = Source(
        id="src_a",
        space_id="gls_q",
        title="Vortrag A",
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline="...",
        external_uri="https://example.com/a",
        created_at=now,
    )
    await db.sources.insert_one(src.model_dump())

    page_allianz = Page(
        space_id="gls_q",
        path="entities/company/allianz",
        kind=PageKind.ENTITY,
        title="Allianz",
        source_refs=["src_a"],
        updated_at=now,
    )
    await db.pages.insert_one(page_allianz.model_dump())
    await storage.write_page(
        "gls_q",
        "pages/entities/company/allianz.md",
        "# Allianz\n\nDie Allianz bietet Cyberversicherung für KMU ([[summaries/src-src_a]]).",
    )
    await storage.write_page(
        "gls_q",
        "index.md",
        "# Index\n\n## Entities — Company\n- [[entities/company/allianz]] — Allianz — 1 source\n",
    )
    return space


async def test_query_returns_answer_with_citations(storage, settings):
    space = await _seed_wiki(storage)

    route = json.dumps(
        {
            "pages_to_load": ["entities/company/allianz"],
            "reasoning": "directly about Allianz",
        }
    )
    answer = "Allianz bietet Cyberversicherung für KMU an ([[entities/company/allianz]])."

    llm = FakeLLMDriver([route, answer])
    response = await answer_question(
        space_id=space.id,
        request=QueryRequest(question="Was bietet Allianz an?"),
        storage=storage,
        settings=settings,
        llm=llm,
    )

    assert response.pages_consulted == ["entities/company/allianz"]
    assert response.cited_pages == ["entities/company/allianz"]
    assert response.cited_sources[0].id == "src_a"
    assert response.cited_sources[0].external_uri == "https://example.com/a"
    assert "Cyberversicherung" in response.answer


async def test_query_empty_wiki_returns_no_answer(storage, settings):
    space = await _seed_wiki(storage)

    route = json.dumps({"pages_to_load": [], "reasoning": "nothing matches"})
    llm = FakeLLMDriver([route])

    response = await answer_question(
        space_id=space.id,
        request=QueryRequest(question="Was bietet Munich Re an?"),
        storage=storage,
        settings=settings,
        llm=llm,
    )
    assert response.pages_consulted == []
    assert response.cited_pages == []
    assert response.cited_sources == []
    assert "keine passenden" in response.answer.lower() or "no" in response.answer.lower()
