"""End-to-end ingest tests using Pydantic AI test models and in-memory storage.

The single-shot ``extract_agent`` is driven with ``TestModel`` (canned structured
output). The agentic ``maintainer_agent`` is driven with a ``FunctionModel`` that
emits a scripted batch of tool calls once, then returns the final report — letting
us assert the surgical-edit / dedup / synthesis behavior deterministically.
"""

from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from glossa.db.client import get_db
from glossa.ingest.agents import extract_agent, maintainer_agent
from glossa.ingest.workflow import run_ingest
from glossa.models.job import Job, JobKind, JobStatus
from glossa.models.source import Source, SourceIngestionMode, SourceStatus
from glossa.models.space import Space


async def _seed_space_and_source(storage, *, source_id="src_one", title="Vortrag: Cyberversicherung bei KMU"):
    db = get_db()
    now = datetime.now(UTC)
    if not await db.spaces.find_one({"id": "gls_test"}):
        space = Space(
            id="gls_test",
            tenant_id="t1",
            name="Test",
            slug="test",
            bucket_uri="mem://gls_test/",
            created_at=now,
            updated_at=now,
        )
        await db.spaces.insert_one(space.model_dump())
        await storage.init_space("gls_test")
    else:
        space = Space.model_validate(await db.spaces.find_one({"id": "gls_test"}))

    source = Source(
        id=source_id,
        space_id="gls_test",
        title=title,
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline=(
            "Bei der User Group Cyber 2024 berichtete Max Mustermann von der "
            "Allianz über Cyberversicherungsprodukte für KMU."
        ),
        external_uri="https://example.com/vortrag/1",
        metadata={"event": "User Group Cyber 2024", "year": "2024"},
        created_at=now,
    )
    await db.sources.insert_one(source.model_dump())

    job = Job(
        id=f"job_{source_id}",
        space_id="gls_test",
        kind=JobKind.INGEST,
        inputs={"source_id": source_id},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job.model_dump())
    return space, source, job


def _extraction_model(entities, summary, blurb):
    return TestModel(
        custom_output_args={
            "entities": entities,
            "source_summary_markdown": summary,
            "log_blurb": blurb,
        },
        call_tools=[],
    )


def _maintainer_model(tool_calls, *, log_blurb="updated wiki"):
    """FunctionModel that emits ``tool_calls`` once, then finishes with a report.

    ``tool_calls`` is a list of ``(tool_name, args_dict)``.
    """

    def fn(messages, info):
        already_called = any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", []))
        if not already_called:
            return ModelResponse(parts=[ToolCallPart(tool_name=n, args=a) for n, a in tool_calls])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"log_blurb": log_blurb, "notes": ""})]
        )

    return FunctionModel(fn)


def _never_finishes_model():
    """FunctionModel that keeps calling a read tool forever (to trigger the step cap)."""

    def fn(messages, info):
        return ModelResponse(parts=[ToolCallPart(tool_name="read_index", args={})])

    return FunctionModel(fn)


async def test_ingest_creates_pages_and_advances_state(storage, settings):
    space, source, job = await _seed_space_and_source(storage)

    extract = _extraction_model(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "neue Cyber-Tarife für KMU",
            },
            {
                "type": "topic",
                "title": "Cyberversicherung",
                "slug": "cyberversicherung",
                "page_path": "entities/topic/cyberversicherung",
                "relevance": "Hauptthema des Vortrags",
            },
        ],
        summary="# Cyberversicherung bei KMU\n\nDie Allianz präsentierte neue Tarife.",
        blurb="Vortrag zu Cyberversicherung bei KMU eingelesen",
    )
    maintain = _maintainer_model(
        [
            (
                "create_page",
                {
                    "path": "entities/company/allianz",
                    "kind": "entity",
                    "title": "Allianz",
                    "body": "# Allianz\n\nDie Allianz präsentierte 2024 neue Cyber-Tarife für KMU "
                    "([[summaries/src-src_one]]).",
                },
            ),
            (
                "create_page",
                {
                    "path": "entities/topic/cyberversicherung",
                    "kind": "entity",
                    "title": "Cyberversicherung",
                    "body": "# Cyberversicherung\n\nAktuelle Entwicklungen bei [[entities/company/allianz]] "
                    "([[summaries/src-src_one]]).",
                },
            ),
        ]
    )

    with extract_agent.override(model=extract), maintainer_agent.override(model=maintain):
        result = await run_ingest(
            job_id=job.id, space_id=space.id, source_id=source.id, storage=storage, settings=settings
        )

    assert sorted(result.pages_created) == sorted(
        ["entities/company/allianz", "entities/topic/cyberversicherung", "summaries/src-src_one"]
    )

    allianz_file = await storage.read_page(space.id, "pages/entities/company/allianz.md")
    assert "Allianz" in allianz_file
    assert "[[summaries/src-src_one]]" in allianz_file

    summary_file = await storage.read_page(space.id, "pages/summaries/src-src_one.md")
    assert "Cyberversicherung bei KMU" in summary_file
    assert "[[entities/company/allianz]]" in summary_file

    index = await storage.read_page(space.id, "index.md")
    assert "[[entities/company/allianz]]" in index
    assert "[[entities/topic/cyberversicherung]]" in index
    assert "[[summaries/src-src_one]]" in index

    log = await storage.read_page(space.id, "log.md")
    assert "ingest |" in log
    assert "Vortrag" in log
    assert "[[entities/company/allianz]]" in log

    db = get_db()
    job_doc = await db.jobs.find_one({"id": job.id})
    assert job_doc["status"] == JobStatus.SUCCEEDED.value
    assert job_doc["ended_at"] is not None

    source_doc = await db.sources.find_one({"id": source.id})
    assert source_doc["status"] == SourceStatus.DONE.value
    assert source_doc["last_ingest_job_id"] == job.id

    pages = await db.pages.count_documents({"space_id": space.id})
    assert pages == 3


async def test_ingest_second_source_updates_existing_entity(storage, settings):
    """A second source editing an existing entity marks it updated, not created, and merges refs."""
    space, source, job = await _seed_space_and_source(storage)

    extract1 = _extraction_model(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "first",
            }
        ],
        summary="First summary",
        blurb="first ingest",
    )
    maintain1 = _maintainer_model(
        [
            (
                "create_page",
                {
                    "path": "entities/company/allianz",
                    "kind": "entity",
                    "title": "Allianz",
                    "body": "# Allianz\n\nFirst entry ([[summaries/src-src_one]]).",
                },
            )
        ]
    )
    with extract_agent.override(model=extract1), maintainer_agent.override(model=maintain1):
        await run_ingest(job_id=job.id, space_id=space.id, source_id=source.id, storage=storage, settings=settings)

    _space, source2, job2 = await _seed_space_and_source(storage, source_id="src_two", title="Whitepaper")

    extract2 = _extraction_model(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "second study",
            }
        ],
        summary="Second summary",
        blurb="second ingest",
    )
    maintain2 = _maintainer_model(
        [
            (
                "add_section",
                {
                    "path": "entities/company/allianz",
                    "heading": "Studie 2024",
                    "content": "Allianz veröffentlichte eine zweite Studie ([[summaries/src-src_two]]).",
                },
            )
        ]
    )
    with extract_agent.override(model=extract2), maintainer_agent.override(model=maintain2):
        result = await run_ingest(
            job_id=job2.id, space_id=space.id, source_id=source2.id, storage=storage, settings=settings
        )

    assert "entities/company/allianz" in result.pages_updated
    assert "entities/company/allianz" not in result.pages_created

    db = get_db()
    page_doc = await db.pages.find_one({"space_id": space.id, "path": "entities/company/allianz"})
    assert set(page_doc["source_refs"]) == {"src_one", "src_two"}

    allianz_file = await storage.read_page(space.id, "pages/entities/company/allianz.md")
    assert "First entry" in allianz_file  # surgical edit preserved existing content
    assert "Studie 2024" in allianz_file
    assert "[[summaries/src-src_two]]" in allianz_file


async def test_ingest_maintainer_dedups_via_search(storage, settings):
    """The maintainer searches, finds the existing entity, and edits it instead of creating a duplicate."""
    space, source, job = await _seed_space_and_source(storage)
    extract1 = _extraction_model(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "first",
            }
        ],
        summary="First",
        blurb="first",
    )
    maintain1 = _maintainer_model(
        [
            (
                "create_page",
                {
                    "path": "entities/company/allianz",
                    "kind": "entity",
                    "title": "Allianz",
                    "body": "# Allianz\n\nFirst ([[summaries/src-src_one]]).",
                },
            )
        ]
    )
    with extract_agent.override(model=extract1), maintainer_agent.override(model=maintain1):
        await run_ingest(job_id=job.id, space_id=space.id, source_id=source.id, storage=storage, settings=settings)

    # Second source: extraction suggests a near-duplicate path; the maintainer
    # searches, finds the canonical page, and edits THAT one.
    _s, source2, job2 = await _seed_space_and_source(storage, source_id="src_two", title="Profil")
    extract2 = _extraction_model(
        entities=[
            {
                "type": "organization",
                "title": "Allianz SE",
                "slug": "allianz-se",
                "page_path": "entities/organization/allianz-se",
                "relevance": "duplicate of existing Allianz",
            }
        ],
        summary="Second",
        blurb="second",
    )
    maintain2 = _maintainer_model(
        [
            ("search_pages", {"query": "allianz"}),
            (
                "add_section",
                {
                    "path": "entities/company/allianz",
                    "heading": "Weitere Quelle",
                    "content": "Ergänzende Angaben ([[summaries/src-src_two]]).",
                },
            ),
        ]
    )
    with extract_agent.override(model=extract2), maintainer_agent.override(model=maintain2):
        result = await run_ingest(
            job_id=job2.id, space_id=space.id, source_id=source2.id, storage=storage, settings=settings
        )

    db = get_db()
    assert await db.pages.find_one({"space_id": space.id, "path": "entities/organization/allianz-se"}) is None
    assert "entities/company/allianz" in result.pages_updated
    page_doc = await db.pages.find_one({"space_id": space.id, "path": "entities/company/allianz"})
    assert set(page_doc["source_refs"]) == {"src_one", "src_two"}


async def test_ingest_creates_synthesis_page(storage, settings):
    space, source, job = await _seed_space_and_source(storage)
    extract = _extraction_model(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "insurer",
            },
            {
                "type": "topic",
                "title": "Cyberversicherung",
                "slug": "cyberversicherung",
                "page_path": "entities/topic/cyberversicherung",
                "relevance": "product",
            },
        ],
        summary="Allianz and Cyberversicherung",
        blurb="ingest",
    )
    maintain = _maintainer_model(
        [
            (
                "create_page",
                {
                    "path": "entities/company/allianz",
                    "kind": "entity",
                    "title": "Allianz",
                    "body": "# Allianz\n\nInsurer ([[summaries/src-src_one]]).",
                },
            ),
            (
                "create_page",
                {
                    "path": "entities/topic/cyberversicherung",
                    "kind": "entity",
                    "title": "Cyberversicherung",
                    "body": "# Cyberversicherung\n\nProduct ([[summaries/src-src_one]]).",
                },
            ),
            (
                "create_page",
                {
                    "path": "syntheses/allianz-cyber",
                    "kind": "synthesis",
                    "title": "Allianz & Cyberversicherung",
                    "body": "# Allianz & Cyberversicherung\n\n[[entities/company/allianz]] bietet "
                    "[[entities/topic/cyberversicherung]] an ([[summaries/src-src_one]]).",
                },
            ),
        ]
    )
    with extract_agent.override(model=extract), maintainer_agent.override(model=maintain):
        result = await run_ingest(
            job_id=job.id, space_id=space.id, source_id=source.id, storage=storage, settings=settings
        )

    assert "syntheses/allianz-cyber" in result.pages_created
    synth = await storage.read_page(space.id, "pages/syntheses/allianz-cyber.md")
    assert "kind: synthesis" in synth
    assert "[[entities/company/allianz]]" in synth
    index = await storage.read_page(space.id, "index.md")
    assert "[[syntheses/allianz-cyber]]" in index


async def test_ingest_step_cap_partial_then_succeeds(storage, settings):
    """Hitting the step cap ends the run cleanly: the job still succeeds and the log notes it."""
    settings.ingest_max_agent_steps = 2
    space, source, job = await _seed_space_and_source(storage)
    extract = _extraction_model(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "x",
            }
        ],
        summary="S",
        blurb="b",
    )
    with extract_agent.override(model=extract), maintainer_agent.override(model=_never_finishes_model()):
        result = await run_ingest(
            job_id=job.id, space_id=space.id, source_id=source.id, storage=storage, settings=settings
        )

    db = get_db()
    assert (await db.jobs.find_one({"id": job.id}))["status"] == JobStatus.SUCCEEDED.value
    assert "partial" in result.log_entry
    # The summary page is still written even when the maintainer is capped.
    assert "summaries/src-src_one" in result.pages_created


async def test_ingest_extract_failure_raises(storage, settings):
    """An extract model that never produces valid structured output fails the run."""
    space, source, job = await _seed_space_and_source(storage)

    def _bad(messages, info):
        return ModelResponse(parts=[])  # no output, no tool calls

    with extract_agent.override(model=FunctionModel(_bad)), pytest.raises(Exception):  # noqa: B017
        await run_ingest(job_id=job.id, space_id=space.id, source_id=source.id, storage=storage, settings=settings)
