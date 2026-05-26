"""End-to-end ingest test using a fake LLM and in-memory storage."""

import json
from datetime import UTC, datetime

import pytest

from glossa.db.client import get_db
from glossa.ingest.workflow import run_ingest
from glossa.models.job import Job, JobKind, JobStatus
from glossa.models.source import Source, SourceIngestionMode, SourceStatus
from glossa.models.space import Space
from glossa.utils.json_parse import LLMJSONError
from tests.fake_llm import FakeLLMDriver


async def _seed_space_and_source(storage):
    db = get_db()
    now = datetime.now(UTC)
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

    source = Source(
        id="src_one",
        space_id="gls_test",
        title="Vortrag: Cyberversicherung bei KMU",
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline=(
            "Bei der User Group Cyber 2024 berichtete Max Mustermann von der "
            "Allianz über Cyberversicherungsprodukte für KMU. Die Allianz hat "
            "neue Tarife entwickelt, die auf Schadenprävention setzen."
        ),
        external_uri="https://example.com/vortrag/1",
        metadata={"event": "User Group Cyber 2024", "year": "2024"},
        created_at=now,
    )
    await db.sources.insert_one(source.model_dump())

    job = Job(
        id="job_one",
        space_id="gls_test",
        kind=JobKind.INGEST,
        inputs={"source_id": "src_one"},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job.model_dump())
    return space, source, job


def _extract_response(entities, summary, blurb):
    return json.dumps(
        {
            "entities": entities,
            "source_summary_markdown": summary,
            "log_blurb": blurb,
        }
    )


def _update_page_response(content):
    return json.dumps(
        {
            "new_content": content,
            "is_changed": True,
            "change_summary": "added new claims from source",
        }
    )


async def test_ingest_creates_pages_and_advances_state(storage, settings):
    space, source, job = await _seed_space_and_source(storage)

    extract = _extract_response(
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
        summary="# Cyberversicherung bei KMU\n\nDie Allianz präsentierte neue Tarife mit Schadenprävention.",
        blurb="Vortrag zu Cyberversicherung bei KMU eingelesen",
    )

    page_allianz = (
        "---\n"
        "kind: entity\n"
        "entity_type: company\n"
        "title: Allianz\n"
        "source_refs: [src_one]\n"
        "updated_at: 2026-05-13T12:00:00Z\n"
        "---\n\n"
        "# Allianz\n\n"
        "Die Allianz präsentierte 2024 neue Cyber-Tarife für KMU "
        "([[summaries/src-src_one]])."
    )
    page_cyber = (
        "---\n"
        "kind: entity\n"
        "entity_type: topic\n"
        "title: Cyberversicherung\n"
        "source_refs: [src_one]\n"
        "updated_at: 2026-05-13T12:00:00Z\n"
        "---\n\n"
        "# Cyberversicherung\n\n"
        "Aktuelle Entwicklungen bei [[entities/company/allianz]] "
        "([[summaries/src-src_one]])."
    )

    llm = FakeLLMDriver([extract, _update_page_response(page_allianz), _update_page_response(page_cyber)])

    result = await run_ingest(
        job_id=job.id,
        space_id=space.id,
        source_id=source.id,
        storage=storage,
        settings=settings,
        llm=llm,
    )

    assert len(llm.calls) == 3
    assert sorted(result.pages_created) == sorted(
        [
            "entities/company/allianz",
            "entities/topic/cyberversicherung",
            "summaries/src-src_one",
        ]
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
    """Second ingest on an existing entity should mark it as updated, not created."""
    space, source, job = await _seed_space_and_source(storage)

    extract1 = _extract_response(
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
    initial_allianz = (
        "---\nkind: entity\nentity_type: company\ntitle: Allianz\n"
        "source_refs: [src_one]\nupdated_at: 2026-05-13T12:00:00Z\n---\n\n"
        "# Allianz\n\nFirst entry."
    )
    llm1 = FakeLLMDriver([extract1, _update_page_response(initial_allianz)])
    await run_ingest(
        job_id=job.id,
        space_id=space.id,
        source_id=source.id,
        storage=storage,
        settings=settings,
        llm=llm1,
    )

    db = get_db()
    now = datetime.now(UTC)
    src2 = Source(
        id="src_two",
        space_id="gls_test",
        title="Whitepaper",
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline="Allianz hat eine zweite Studie veröffentlicht.",
        created_at=now,
    )
    await db.sources.insert_one(src2.model_dump())
    job2 = Job(
        id="job_two",
        space_id="gls_test",
        kind=JobKind.INGEST,
        inputs={"source_id": "src_two"},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job2.model_dump())

    extract2 = _extract_response(
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
    updated_allianz = (
        "---\nkind: entity\nentity_type: company\ntitle: Allianz\n"
        "source_refs: [src_one, src_two]\nupdated_at: 2026-05-13T13:00:00Z\n---\n\n"
        "# Allianz\n\nFirst entry. Plus new study ([[summaries/src-src_two]])."
    )
    llm2 = FakeLLMDriver([extract2, _update_page_response(updated_allianz)])
    result = await run_ingest(
        job_id=job2.id,
        space_id=space.id,
        source_id=src2.id,
        storage=storage,
        settings=settings,
        llm=llm2,
    )

    assert "entities/company/allianz" in result.pages_updated
    assert "entities/company/allianz" not in result.pages_created

    page_doc = await db.pages.find_one({"space_id": space.id, "path": "entities/company/allianz"})
    assert set(page_doc["source_refs"]) == {"src_one", "src_two"}


async def test_ingest_failure_marks_job_failed(storage, settings):
    space, source, job = await _seed_space_and_source(storage)

    llm = FakeLLMDriver(["this is not json"])
    with pytest.raises(LLMJSONError):
        await run_ingest(
            job_id=job.id,
            space_id=space.id,
            source_id=source.id,
            storage=storage,
            settings=settings,
            llm=llm,
        )
