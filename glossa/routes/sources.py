import json
import os
import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from glossa.auth import AuthContext, get_auth_context, space_query
from glossa.db.client import get_db
from glossa.ingest.workflow import enqueue_ingest
from glossa.models.job import Job
from glossa.models.source import Source, SourceCreate, SourceIngestionMode, SourceStatus
from glossa.usage.quota import QuotaExceededError, check_quota, check_source_quota
from glossa.utils.slug import slugify

router = APIRouter(prefix="/spaces/{space_id}/sources", tags=["sources"])


def _safe_asset_filename(filename: str | None) -> str:
    """Slugify the stem and keep a sane file extension for storage."""
    name = os.path.basename(filename or "upload")
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ""
    return f"{slugify(stem) or 'file'}{ext}"


@router.post("", response_model=Source)
async def create_source(
    space_id: str,
    body: SourceCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Source:
    db = get_db()
    space_doc = await db.spaces.find_one(space_query(space_id, ctx), {"id": 1, "tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    # System contexts (self-host / bootstrap admin) are not real tenants and
    # are exempt from per-space source quotas.
    if not ctx.is_system:
        try:
            await check_source_quota(space_doc["tenant_id"], space_id)
        except QuotaExceededError as e:
            raise HTTPException(
                status_code=402,
                detail={"reason": e.reason, "quota": e.status.model_dump()},
            ) from e
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


@router.post("/upload", response_model=Source)
async def upload_source(
    space_id: str,
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    file: Annotated[UploadFile, File(...)],
    title: Annotated[str | None, Form()] = None,
    external_uri: Annotated[str | None, Form()] = None,
    metadata: Annotated[str | None, Form()] = None,
) -> Source:
    """Upload a document (PDF/DOCX/PPTX/…) as an ``upload``-mode source.

    The raw file is stored in object storage; it is parsed to text with
    LiteParse during the subsequent ``POST .../ingest`` call.
    """
    db = get_db()
    space_doc = await db.spaces.find_one(space_query(space_id, ctx), {"id": 1, "tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    if not ctx.is_system:
        try:
            await check_source_quota(space_doc["tenant_id"], space_id)
        except QuotaExceededError as e:
            raise HTTPException(
                status_code=402,
                detail={"reason": e.reason, "quota": e.status.model_dump()},
            ) from e

    settings = request.app.state.settings
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(data) > settings.ingest_max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds max upload size of {settings.ingest_max_upload_bytes} bytes",
        )

    extra_metadata: dict = {}
    if metadata:
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=422, detail="metadata must be a JSON object") from e
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="metadata must be a JSON object")
        extra_metadata = parsed

    source_id = f"src_{uuid4().hex[:12]}"
    asset_path = f"assets/{source_id}/{_safe_asset_filename(file.filename)}"
    await request.app.state.storage.write_asset(
        space_id, asset_path, data, file.content_type or "application/octet-stream"
    )

    source = Source(
        id=source_id,
        space_id=space_id,
        title=title or file.filename or "Uploaded document",
        ingestion_mode=SourceIngestionMode.UPLOAD,
        external_uri=external_uri,
        asset_path=asset_path,
        metadata={
            **extra_metadata,
            "filename": file.filename,
            "content_type": file.content_type,
            "byte_size": len(data),
        },
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
