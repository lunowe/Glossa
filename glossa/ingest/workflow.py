"""End-to-end ingest workflow.

``enqueue_ingest`` persists a Job and starts a background task. The task runs
``run_ingest`` which executes the workflow step by step, updating the Job
record after each phase and firing webhooks on completion.

The pipeline: fetch the source, extract entities + a summary (single LLM call),
write the per-source summary page, then run the agentic **maintainer** which
edits the wiki with surgical patch tools (dedup, synthesis, minimal edits) under
caps; the maintainer's working copy is flushed deterministically, then the index
and log are regenerated.

For MVP this is in-process. The Job model and asyncio.Task split makes it easy to
move to a real worker (Arq/RQ/Celery) without changing the API contract.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI

from glossa.concurrency import lock_for_space, track_background_task
from glossa.db.client import get_db
from glossa.ingest import agents, index_writer, log_writer, page_writer, source_fetcher, webhook_delivery
from glossa.ingest.extract import extract_from_source
from glossa.llm import build_model, model_settings_for, resolve_model_name, resolve_provider, usage_to_dict
from glossa.models.job import Job, JobKind, JobResult, JobStatus
from glossa.models.page import PageKind
from glossa.models.source import Source, SourceStatus
from glossa.models.space import Space
from glossa.models.webhook import WebhookEvent
from glossa.usage import Operation, record_usage

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from glossa.config import Settings
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
            model=None,
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
    model: "Model | None",
) -> None:
    try:
        await run_ingest(
            job_id=job_id,
            space_id=space_id,
            source_id=source_id,
            storage=storage,
            settings=settings,
            model=model,
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
    model: "Model | None" = None,
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
            model=model,
        )


async def _run_ingest_inner(
    *,
    job_id: str,
    space_id: str,
    source_id: str,
    storage: "StorageBackend",
    settings: "Settings",
    model: "Model | None",
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

    if model is None:
        model = build_model(space, settings)
    provider = resolve_provider(space, settings)
    effective_model = resolve_model_name(space, settings)
    settings_for_call = model_settings_for(space, settings, temperature=0.2)

    await db.sources.update_one(
        {"id": source_id},
        {"$set": {"status": SourceStatus.INGESTING.value}},
    )

    schema_markdown = await storage.read_page(space_id, "schema.md") or ""
    source_content = await source_fetcher.fetch_content(
        source, settings.ingest_max_source_chars, storage=storage, settings=settings
    )

    # 1. Extract (single-shot): entities + a self-contained summary.
    extraction = await extract_from_source(
        model=model,
        model_settings=settings_for_call,
        provider=provider,
        model_name=effective_model,
        schema_markdown=schema_markdown,
        source={
            "id": source.id,
            "title": source.title,
            "external_uri": source.external_uri,
            "metadata": source.metadata,
        },
        source_content=source_content,
    )
    await record_usage(
        tenant_id=space.tenant_id,
        space_id=space.id,
        operation=Operation.INGEST_EXTRACT,
        model=effective_model,
        usage=extraction.usage,
        job_id=job_id,
    )

    pages_created: list[str] = []
    pages_updated: list[str] = []

    # 2. Summary page first, so the maintainer's edits can cite [[summaries/src-<id>]].
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
        tenant_id=space.tenant_id,
    )
    if sum_is_new:
        pages_created.append(summary_path)
    else:
        pages_updated.append(summary_path)

    # 3. Maintainer agent: surgical, dedup-aware edits + synthesis (capped).
    entities = [
        {"type": e.type, "title": e.title, "page_path": e.page_path, "relevance": e.relevance}
        for e in extraction.entities
    ]
    wc, report, maintainer_usage, capped = await agents.run_maintainer(
        model=model,
        model_settings=settings_for_call,
        retries=settings.ingest_agent_retries,
        space=space,
        source=source,
        source_summary_markdown=extraction.source_summary_markdown,
        entities=entities,
        schema_markdown=schema_markdown,
        storage=storage,
        settings=settings,
    )
    if maintainer_usage is not None:
        await record_usage(
            tenant_id=space.tenant_id,
            space_id=space.id,
            operation=Operation.INGEST_UPDATE_PAGE,
            model=effective_model,
            usage=usage_to_dict(maintainer_usage, provider=provider),
            job_id=job_id,
        )

    # 4. Flush the working copy deterministically (validate, stamp, quota, write).
    created, updated = await agents.flush_working_copy(
        wc=wc, space=space, source=source, job_id=job_id, storage=storage
    )
    pages_created.extend(created)
    pages_updated.extend(updated)

    # 5. Index + log (deterministic).
    await index_writer.regenerate_index(storage=storage, space_id=space_id)
    log_blurb = report.log_blurb.strip() if report and report.log_blurb.strip() else extraction.log_blurb
    if capped:
        log_blurb = f"{log_blurb} [partial: ingest step cap reached]".strip()
    await log_writer.append_log_entry(
        storage=storage,
        space_id=space_id,
        kind="ingest",
        title=source.title,
        pages_created=pages_created,
        pages_updated=pages_updated,
        summary_path=summary_path,
        note=log_blurb,
    )

    result = JobResult(
        pages_created=pages_created,
        pages_updated=pages_updated,
        contradictions_flagged=[],
        log_entry=log_blurb,
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
