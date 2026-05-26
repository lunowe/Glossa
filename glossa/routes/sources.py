from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from glossa.auth import AuthContext, get_auth_context, space_query
from glossa.db.client import get_db
from glossa.ingest.workflow import enqueue_ingest
from glossa.models.job import Job
from glossa.models.source import Source, SourceCreate, SourceStatus
from glossa.usage.quota import QuotaExceededError, check_quota

router = APIRouter(prefix="/spaces/{space_id}/sources", tags=["sources"])


@router.post("", response_model=Source)
async def create_source(
    space_id: str,
    body: SourceCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Source:
    db = get_db()
    if not await db.spaces.find_one(space_query(space_id, ctx), {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    source = Source(
        id=f"src_{uuid4().hex[:12]}",
        space_id=space_id,
        title=body.title,
        ingestion_mode=body.ingestion_mode,
        content_inline=body.content_inline,
        fetch_callback=body.fetch_callback,
        external_uri=body.external_uri,
        metadata=body.metadata,
        status=SourceStatus.RECEIVED,
        created_at=datetime.now(UTC),
    )
    await db.sources.insert_one(source.model_dump())
    return source


@router.get("", response_model=list[Source])
async def list_sources(
    space_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    limit: int = 50,
    offset: int = 0,
) -> list[Source]:
    db = get_db()
    if not await db.spaces.find_one(space_query(space_id, ctx), {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    cursor = db.sources.find({"space_id": space_id}).skip(offset).limit(limit)
    return [Source.model_validate(doc) async for doc in cursor]


@router.get("/{source_id}", response_model=Source)
async def get_source(
    space_id: str,
    source_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Source:
    db = get_db()
    if not await db.spaces.find_one(space_query(space_id, ctx), {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    doc = await db.sources.find_one({"id": source_id, "space_id": space_id})
    if not doc:
        raise HTTPException(status_code=404, detail="source not found")
    return Source.model_validate(doc)


@router.post("/{source_id}/ingest", response_model=Job)
async def ingest_source(
    space_id: str,
    source_id: str,
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Job:
    db = get_db()
    space_doc = await db.spaces.find_one(space_query(space_id, ctx), {"tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    source_doc = await db.sources.find_one({"id": source_id, "space_id": space_id})
    if not source_doc:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        await check_quota(space_doc["tenant_id"])
    except QuotaExceededError as e:
        raise HTTPException(status_code=402, detail={"reason": e.reason, "quota": e.status.model_dump()}) from e
    job = await enqueue_ingest(space_id=space_id, source_id=source_id, app=request.app)
    return job
