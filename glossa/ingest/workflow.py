"""End-to-end ingest workflow.

``enqueue_ingest`` persists a Job and starts a background task. The task runs
``run_ingest`` which executes the workflow step by step, updating the Job
record after each phase and firing webhooks on completion.

For MVP this is in-process. The Job model and asyncio.Task split makes it
easy to move to a real worker (Arq/RQ/Celery) without changing the API
contract.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI

from glossa.concurrency import lock_for_space, track_background_task
from glossa.db.client import get_db
from glossa.ingest import index_writer, log_writer, page_writer, source_fetcher, webhook_delivery
from glossa.ingest.extract import extract_from_source
from glossa.llm import build_driver
from glossa.models.job import Job, JobKind, JobResult, JobStatus
from glossa.models.page import PageKind
from glossa.models.source import Source, SourceStatus
from glossa.models.space import LLMMode, Space
from glossa.models.webhook import WebhookEvent
from glossa.usage import Operation, record_usage

if TYPE_CHECKING:
    from glossa.config import Settings
    from glossa.llm.base import LLMDriver
    from glossa.storage.base import StorageBackend

logger = logging.getLogger(__name__)


async def enqueue_ingest(*, space_id: str, source_id: str, app: FastAPI) -> Job:
    db = get_db()
    job = Job(
        id=f"job_{uuid4().hex[:12]}",
        space_id=space_id,
        kind=JobKind.INGEST,
        inputs={"source_id": source_id},
        status=JobStatus.QUEUED,
        created_at=datetime.now(UTC),
    )
    await db.jobs.insert_one(job.model_dump())

    settings = app.state.settings
    storage = app.state.storage
    task = asyncio.create_task(
        _run_ingest_safely(
            job_id=job.id,
            space_id=space_id,
            source_id=source_id,
            storage=storage,
            settings=settings,
            llm=None,
        )
    )
    track_background_task(task)
    return job


async def _run_ingest_safely(
    *,
    job_id: str,
    space_id: str,
    source_id: str,
    storage: "StorageBackend",
    settings: "Settings",
    llm: "LLMDriver | None",
) -> None:
    try:
        await run_ingest(
            job_id=job_id,
            space_id=space_id,
            source_id=source_id,
            storage=storage,
            settings=settings,
            llm=llm,
        )
    except Exception as e:
        logger.exception("ingest job %s failed", job_id)
        await _mark_job_failed(job_id, repr(e))
        await webhook_delivery.fire(
            space_id=space_id,
            event=WebhookEvent.JOB_FAILED,
            payload={"job_id": job_id, "error": repr(e)},
        )


async def run_ingest(
    *,
    job_id: str,
    space_id: str,
    source_id: str,
    storage: "StorageBackend",
    settings: "Settings",
    llm: "LLMDriver | None" = None,
) -> JobResult:
    """Execute one ingest job to completion.

    Serialized per space via an in-memory lock. Updates Job status as it
    progresses; returns the JobResult on success. Raises on failure (the
    caller is expected to record the failure on the Job).
    """
    async with lock_for_space(space_id):
        return await _run_ingest_inner(
            job_id=job_id,
            space_id=space_id,
            source_id=source_id,
            storage=storage,
            settings=settings,
            llm=llm,
        )


async def _run_ingest_inner(
    *,
    job_id: str,
    space_id: str,
    source_id: str,
    storage: "StorageBackend",
    settings: "Settings",
    llm: "LLMDriver | None",
) -> JobResult:
    db = get_db()
    started_at = datetime.now(UTC)
    await db.jobs.update_one(
        {"id": job_id},
        {"$set": {"status": JobStatus.RUNNING.value, "started_at": started_at}},
    )

    space_doc = await db.spaces.find_one({"id": space_id})
    if not space_doc:
        raise RuntimeError(f"space {space_id} not found")
    space = Space.model_validate(space_doc)

    source_doc = await db.sources.find_one({"id": source_id, "space_id": space_id})
    if not source_doc:
        raise RuntimeError(f"source {source_id} not found in space {space_id}")
    source = Source.model_validate(source_doc)

    if llm is None:
        llm = build_driver(space, settings)

    effective_model = _resolve_effective_model(space, settings)

    await db.sources.update_one(
        {"id": source_id},
        {"$set": {"status": SourceStatus.INGESTING.value}},
    )

    schema_markdown = await storage.read_page(space_id, "schema.md") or ""
    source_content = await source_fetcher.fetch_content(source, settings.ingest_max_source_chars)

    extraction = await extract_from_source(
        llm=llm,
        schema_markdown=schema_markdown,
        source={
            "id": source.id,
            "title": source.title,
            "external_uri": source.external_uri,
            "metadata": source.metadata,
        },
        source_content=source_content,
        model=effective_model,
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.INGEST_EXTRACT,
        model=extraction.model,
        usage=extraction.usage,
        job_id=job_id,
    )

    pages_created: list[str] = []
    pages_updated: list[str] = []

    for entity in extraction.entities:
        existing = await page_writer.read_existing_page(storage, space_id, entity.page_path)
        update, page_usage = await page_writer.llm_update_entity_page(
            llm=llm,
            schema_markdown=schema_markdown,
            entity=entity,
            existing_page_markdown=existing,
            source_id=source.id,
            source_title=source.title,
            source_summary_markdown=extraction.source_summary_markdown,
        )
        await record_usage(
            tenant_id=space.tenant_id,
            space_id=space.id,
            operation=Operation.INGEST_UPDATE_PAGE,
            model=effective_model,
            usage=page_usage,
            job_id=job_id,
        )
        new_content = str(update["new_content"])

        existing_source_refs = []
        if source_doc:
            existing_page = await db.pages.find_one(
                {"space_id": space_id, "path": entity.page_path}, {"source_refs": 1}
            )
            if existing_page:
                existing_source_refs = existing_page.get("source_refs") or []
        merged_refs = list(dict.fromkeys([*existing_source_refs, source.id]))

        kind = PageKind.ENTITY
        is_new, is_changed = await page_writer.upsert_page(
            storage=storage,
            space_id=space_id,
            page_path=entity.page_path,
            kind=kind,
            title=entity.title,
            new_content=new_content,
            source_refs=merged_refs,
            job_id=job_id,
        )
        if is_new:
            pages_created.append(entity.page_path)
        elif is_changed:
            pages_updated.append(entity.page_path)

    summary_path = f"summaries/src-{source.id}"
    summary_markdown = page_writer.build_summary_page(
        source_id=source.id,
        source_title=source.title,
        source_external_uri=source.external_uri,
        source_metadata=source.metadata,
        summary_markdown=extraction.source_summary_markdown,
        entity_page_paths=[e.page_path for e in extraction.entities],
    )
    sum_is_new, _sum_is_changed = await page_writer.upsert_page(
        storage=storage,
        space_id=space_id,
        page_path=summary_path,
        kind=PageKind.SUMMARY,
        title=source.title,
        new_content=summary_markdown,
        source_refs=[source.id],
        job_id=job_id,
    )
    if sum_is_new:
        pages_created.append(summary_path)
    else:
        pages_updated.append(summary_path)

    await index_writer.regenerate_index(storage=storage, space_id=space_id)
    await log_writer.append_log_entry(
        storage=storage,
        space_id=space_id,
        kind="ingest",
        title=source.title,
        pages_created=pages_created,
        pages_updated=pages_updated,
        summary_path=summary_path,
        note=extraction.log_blurb,
    )

    result = JobResult(
        pages_created=pages_created,
        pages_updated=pages_updated,
        contradictions_flagged=[],
        log_entry=extraction.log_blurb,
    )
    ended_at = datetime.now(UTC)
    await db.jobs.update_one(
        {"id": job_id},
        {
            "$set": {
                "status": JobStatus.SUCCEEDED.value,
                "ended_at": ended_at,
                "result": result.model_dump(),
            },
        },
    )
    await db.sources.update_one(
        {"id": source_id},
        {
            "$set": {
                "status": SourceStatus.DONE.value,
                "last_ingested_at": ended_at,
                "last_ingest_job_id": job_id,
            },
        },
    )
    await db.spaces.update_one(
        {"id": space_id},
        {
            "$set": {"stats.last_ingest_at": ended_at, "updated_at": ended_at},
            "$inc": {"stats.page_count": len(pages_created)},
        },
    )

    await webhook_delivery.fire(
        space_id=space_id,
        event=WebhookEvent.JOB_COMPLETE,
        payload={"job_id": job_id, "source_id": source_id, "result": result.model_dump()},
    )
    return result


def _resolve_effective_model(space: Space, settings: "Settings") -> str:
    """Return the model string that will be used for LLM calls in this space.

    Mirrors the precedence in ``glossa.llm.factory.build_driver`` so usage
    events record the same model the driver actually calls. Used for billing
    attribution.
    """
    cfg = space.llm_config
    if cfg.model:
        return cfg.model
    if cfg.mode == LLMMode.HOSTED:
        return settings.hosted_default_model
    return settings.default_llm_model


async def _mark_job_failed(job_id: str, error_message: str) -> None:
    db = get_db()
    await db.jobs.update_one(
        {"id": job_id},
        {
            "$set": {
                "status": JobStatus.FAILED.value,
                "ended_at": datetime.now(UTC),
                "error_message": error_message,
            },
        },
    )
    job_doc = await db.jobs.find_one({"id": job_id}, {"inputs": 1})
    source_id = (job_doc or {}).get("inputs", {}).get("source_id")
    if source_id:
        await db.sources.update_one(
            {"id": source_id},
            {"$set": {"status": SourceStatus.FAILED.value}},
        )
