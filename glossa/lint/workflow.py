"""End-to-end lint workflow.

``enqueue_lint`` persists a ``Job`` of kind ``lint`` and starts a background
task. The task acquires the per-space lock, runs the deterministic scanner
and (for pages citing ≥2 sources) the LLM contradiction check, writes
``lint_report.md`` and a log entry, and marks the Job done.

Shape mirrors ``ingest.workflow`` so future workers (Arq/RQ) can swap in
without changing the API contract.
"""

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI

from glossa.concurrency import lock_for_space, track_background_task
from glossa.db.client import get_db
from glossa.ingest import webhook_delivery
from glossa.lint import report_writer, scanner
from glossa.lint.contradictions import check_page_for_contradictions
from glossa.llm import build_driver
from glossa.models.job import Job, JobKind, JobResult, JobStatus
from glossa.models.space import LLMMode, Space
from glossa.models.webhook import WebhookEvent
from glossa.usage import Operation, record_usage

if TYPE_CHECKING:
    from glossa.config import Settings
    from glossa.llm.base import LLMDriver
    from glossa.storage.base import StorageBackend

logger = logging.getLogger(__name__)


async def enqueue_lint(*, space_id: str, app: FastAPI) -> Job:
    db = get_db()
    job = Job(
        id=f"job_{uuid4().hex[:12]}",
        space_id=space_id,
        kind=JobKind.LINT,
        inputs={},
        status=JobStatus.QUEUED,
        created_at=datetime.now(UTC),
    )
    await db.jobs.insert_one(job.model_dump())

    settings = app.state.settings
    storage = app.state.storage
    task = asyncio.create_task(
        _run_lint_safely(
            job_id=job.id,
            space_id=space_id,
            storage=storage,
            settings=settings,
            llm=None,
        )
    )
    track_background_task(task)
    return job


async def _run_lint_safely(
    *,
    job_id: str,
    space_id: str,
    storage: "StorageBackend",
    settings: "Settings",
    llm: "LLMDriver | None",
) -> None:
    try:
        await run_lint(
            job_id=job_id,
            space_id=space_id,
            storage=storage,
            settings=settings,
            llm=llm,
        )
    except Exception as e:
        logger.exception("lint job %s failed", job_id)
        await _mark_job_failed(job_id, repr(e))
        await webhook_delivery.fire(
            space_id=space_id,
            event=WebhookEvent.JOB_FAILED,
            payload={"job_id": job_id, "kind": JobKind.LINT.value, "error": repr(e)},
        )


async def run_lint(
    *,
    job_id: str,
    space_id: str,
    storage: "StorageBackend",
    settings: "Settings",
    llm: "LLMDriver | None" = None,
) -> JobResult:
    async with lock_for_space(space_id):
        return await _run_lint_inner(
            job_id=job_id,
            space_id=space_id,
            storage=storage,
            settings=settings,
            llm=llm,
        )


async def _run_lint_inner(
    *,
    job_id: str,
    space_id: str,
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

    schema_markdown = await storage.read_page(space_id, "schema.md") or ""
    pages = await scanner.load_pages(storage, space_id)
    scan_result = scanner.scan_deterministic(pages)

    findings: list[dict] = [
        {
            "category": f.category,
            "page_path": f.page_path,
            "detail": f.detail,
            "related_paths": list(f.related_paths),
        }
        for f in scan_result.findings
    ]

    pages_with_llm_check = 0
    if pages and any(len(p.source_refs) >= 2 for p in pages):
        if llm is None:
            llm = build_driver(space, settings)
        effective_model = _resolve_effective_model(space, settings)

        for page in pages:
            if len(page.source_refs) < 2:
                continue
            pages_with_llm_check += 1
            contradiction_findings, usage = await check_page_for_contradictions(
                llm=llm,
                storage=storage,
                space_id=space_id,
                schema_markdown=schema_markdown,
                page=page,
            )
            if usage is not None:
                await record_usage(
                    tenant_id=space.tenant_id,
                    space_id=space.id,
                    operation=Operation.LINT,
                    model=effective_model,
                    usage=usage,
                    job_id=job_id,
                )
            for cf in contradiction_findings:
                findings.append(
                    {
                        "category": cf.kind,
                        "page_path": cf.page_path,
                        "claim": cf.claim,
                        "detail": cf.claim,
                        "explanation": cf.explanation,
                        "source_ids": list(cf.source_ids),
                        "related_paths": list(cf.related_paths),
                    }
                )

    summary_counts: Counter[str] = Counter(f["category"] for f in findings)
    summary = dict(summary_counts)

    await report_writer.write_report(
        storage=storage,
        space_id=space_id,
        findings=findings,
        pages_scanned=len(pages),
        pages_with_llm_check=pages_with_llm_check,
        job_id=job_id,
    )
    await report_writer.append_lint_log_entry(
        storage=storage,
        space_id=space_id,
        summary=summary,
        job_id=job_id,
    )

    log_entry = (
        f"lint: {len(findings)} finding(s) across {len(pages)} page(s)"
        if findings
        else f"lint: clean across {len(pages)} page(s)"
    )
    result = JobResult(
        lint_findings=findings,
        lint_summary=summary,
        log_entry=log_entry,
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
    await db.spaces.update_one(
        {"id": space_id},
        {"$set": {"updated_at": ended_at}},
    )

    await webhook_delivery.fire(
        space_id=space_id,
        event=WebhookEvent.JOB_COMPLETE,
        payload={"job_id": job_id, "kind": JobKind.LINT.value, "result": result.model_dump()},
    )
    return result


def _resolve_effective_model(space: Space, settings: "Settings") -> str:
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
