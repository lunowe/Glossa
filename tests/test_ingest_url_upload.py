"""Tests for url-mode (pasted links) and upload-mode (documents) ingestion.

The actual network fetch (trafilatura) and document parser (LiteParse) are
monkeypatched — these tests exercise the wiring, not the third-party parsers.
"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from glossa.config import Settings
from glossa.db.client import get_db
from glossa.ingest import doc_parser, source_fetcher, url_fetcher
from glossa.ingest.workflow import run_ingest
from glossa.main import app
from glossa.models.job import Job, JobKind, JobStatus
from glossa.models.source import Source, SourceCreate, SourceIngestionMode, SourceStatus
from glossa.models.space import Space
from glossa.storage.memory import InMemoryStorageBackend
from tests.fake_llm import FakeLLMDriver


async def _seed_space(storage, *, space_id="gls_test", tenant_id="t1"):
    db = get_db()
    now = datetime.now(UTC)
    space = Space(
        id=space_id,
        tenant_id=tenant_id,
        name="Test",
        slug="test",
        bucket_uri=f"mem://{space_id}/",
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space(space_id)
    return space


def _extract_response(entities, summary, blurb):
    return json.dumps({"entities": entities, "source_summary_markdown": summary, "log_blurb": blurb})


def _update_page_response(content):
    return json.dumps({"new_content": content, "is_changed": True, "change_summary": "x"})


def _entity_pipeline_llm():
    """A FakeLLMDriver scripted for: one extract call + one entity page update."""
    extract = _extract_response(
        entities=[
            {
                "type": "company",
                "title": "Allianz",
                "slug": "allianz",
                "page_path": "entities/company/allianz",
                "relevance": "from the fetched/parsed content",
            }
        ],
        summary="# Summary\n\nAllianz appears in the source.",
        blurb="ingested",
    )
    page = (
        "---\nkind: entity\nentity_type: company\ntitle: Allianz\n"
        "source_refs: [src_x]\nupdated_at: 2026-05-13T12:00:00Z\n---\n\n"
        "# Allianz\n\nFrom the source ([[summaries/src-src_x]])."
    )
    return FakeLLMDriver([extract, _update_page_response(page)])


# --- Model validation -----------------------------------------------------------


def test_url_mode_requires_external_uri():
    with pytest.raises(ValidationError, match="url mode requires external_uri"):
        SourceCreate(title="x", ingestion_mode=SourceIngestionMode.URL)


def test_url_mode_with_external_uri_ok():
    sc = SourceCreate(title="x", ingestion_mode=SourceIngestionMode.URL, external_uri="https://e.com/a")
    assert sc.external_uri == "https://e.com/a"


def test_upload_mode_rejected_in_source_create():
    with pytest.raises(ValidationError, match="POST /spaces"):
        SourceCreate(title="x", ingestion_mode=SourceIngestionMode.UPLOAD)


# --- Storage binary assets ------------------------------------------------------


async def test_storage_asset_round_trip():
    storage = InMemoryStorageBackend()
    await storage.write_asset("gls_1", "assets/src-1/a.pdf", b"%PDF-bytes", "application/pdf")
    assert await storage.read_asset("gls_1", "assets/src-1/a.pdf") == b"%PDF-bytes"


async def test_storage_read_missing_asset_raises():
    storage = InMemoryStorageBackend()
    with pytest.raises(FileNotFoundError):
        await storage.read_asset("gls_1", "assets/nope")


async def test_assets_not_listed_as_pages():
    storage = InMemoryStorageBackend()
    await storage.init_space("gls_1")
    await storage.write_asset("gls_1", "assets/src-1/a.pdf", b"x", "application/pdf")
    pages = await storage.list_pages("gls_1", "pages/")
    assert not any("assets/" in p for p in pages)


# --- url-mode ingest ------------------------------------------------------------


async def test_url_ingest_fetches_and_builds_pages(storage, settings, monkeypatch):
    space = await _seed_space(storage)
    db = get_db()
    now = datetime.now(UTC)
    src = Source(
        id="src_x",
        space_id=space.id,
        title="Pasted link",
        ingestion_mode=SourceIngestionMode.URL,
        external_uri="https://example.com/article",
        created_at=now,
    )
    await db.sources.insert_one(src.model_dump())
    job = Job(
        id="job_x",
        space_id=space.id,
        kind=JobKind.INGEST,
        inputs={"source_id": "src_x"},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job.model_dump())

    captured = {}

    async def fake_fetch(url, *, settings):
        captured["url"] = url
        return "# Article\n\nAllianz launched a product."

    monkeypatch.setattr(url_fetcher, "fetch_url_as_markdown", fake_fetch)

    result = await run_ingest(
        job_id=job.id,
        space_id=space.id,
        source_id=src.id,
        storage=storage,
        settings=settings,
        llm=_entity_pipeline_llm(),
    )

    assert captured["url"] == "https://example.com/article"
    assert "entities/company/allianz" in result.pages_created
    allianz = await storage.read_page(space.id, "pages/entities/company/allianz.md")
    assert "Allianz" in allianz
    src_doc = await db.sources.find_one({"id": "src_x"})
    assert src_doc["status"] == SourceStatus.DONE.value


# --- upload-mode ingest ---------------------------------------------------------


async def test_upload_ingest_parses_asset_and_builds_pages(storage, settings, monkeypatch):
    space = await _seed_space(storage)
    db = get_db()
    now = datetime.now(UTC)
    asset_path = "assets/src-x/report.pdf"
    await storage.write_asset(space.id, asset_path, b"%PDF-1.7 raw bytes", "application/pdf")
    src = Source(
        id="src_x",
        space_id=space.id,
        title="report.pdf",
        ingestion_mode=SourceIngestionMode.UPLOAD,
        asset_path=asset_path,
        metadata={"filename": "report.pdf", "content_type": "application/pdf"},
        created_at=now,
    )
    await db.sources.insert_one(src.model_dump())
    job = Job(
        id="job_x",
        space_id=space.id,
        kind=JobKind.INGEST,
        inputs={"source_id": "src_x"},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job.model_dump())

    captured = {}

    async def fake_parse(*, data, filename, settings):
        captured["data"] = data
        captured["filename"] = filename
        return "Allianz Q3 report. Revenue up."

    monkeypatch.setattr(doc_parser, "parse_asset_to_text", fake_parse)

    result = await run_ingest(
        job_id=job.id,
        space_id=space.id,
        source_id=src.id,
        storage=storage,
        settings=settings,
        llm=_entity_pipeline_llm(),
    )

    assert captured["data"] == b"%PDF-1.7 raw bytes"
    assert captured["filename"] == "report.pdf"
    assert "entities/company/allianz" in result.pages_created


async def test_upload_missing_asset_fails_ingest(storage, settings):
    space = await _seed_space(storage)
    db = get_db()
    now = datetime.now(UTC)
    src = Source(
        id="src_x",
        space_id=space.id,
        title="gone.pdf",
        ingestion_mode=SourceIngestionMode.UPLOAD,
        asset_path="assets/src-x/gone.pdf",
        created_at=now,
    )
    await db.sources.insert_one(src.model_dump())
    job = Job(
        id="job_x",
        space_id=space.id,
        kind=JobKind.INGEST,
        inputs={"source_id": "src_x"},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job.model_dump())

    with pytest.raises(source_fetcher.SourceFetchError, match="asset missing"):
        await run_ingest(
            job_id=job.id,
            space_id=space.id,
            source_id=src.id,
            storage=storage,
            settings=settings,
            llm=FakeLLMDriver([]),
        )


# --- HTTP upload endpoint -------------------------------------------------------


def _make_client(*, max_upload_bytes: int = 25_000_000) -> TestClient:
    app.state.settings = Settings(auth_required=False, ingest_max_upload_bytes=max_upload_bytes)
    app.state.storage = InMemoryStorageBackend()
    return TestClient(app)


async def test_upload_endpoint_creates_source_and_stores_asset(mongomock_db):
    client = _make_client()
    await _seed_space(app.state.storage, space_id="gls_http")

    resp = client.post(
        "/spaces/gls_http/sources/upload",
        files={"file": ("Q3 Report.pdf", b"%PDF-1.7 data", "application/pdf")},
        data={"title": "Q3 Report", "metadata": json.dumps({"year": "2026"})},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingestion_mode"] == "upload"
    assert body["title"] == "Q3 Report"
    assert body["asset_path"].startswith("assets/")
    assert body["asset_path"].endswith(".pdf")
    assert body["metadata"]["year"] == "2026"
    assert body["metadata"]["byte_size"] == len(b"%PDF-1.7 data")
    # asset actually persisted under the space prefix
    stored = app.state.storage.assets[f"gls_http/{body['asset_path']}"]
    assert stored == b"%PDF-1.7 data"


async def test_upload_endpoint_rejects_oversized_file(mongomock_db):
    client = _make_client(max_upload_bytes=4)
    await _seed_space(app.state.storage, space_id="gls_http")

    resp = client.post(
        "/spaces/gls_http/sources/upload",
        files={"file": ("big.pdf", b"way too many bytes", "application/pdf")},
    )
    assert resp.status_code == 413


async def test_upload_endpoint_404_for_missing_space(mongomock_db):
    client = _make_client()
    resp = client.post(
        "/spaces/gls_missing/sources/upload",
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert resp.status_code == 404
