from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from glossa.auth import AuthContext, get_auth_context, is_admin, space_query, tenant_scope_filter
from glossa.db.client import get_db
from glossa.models.space import Space, SpaceCreate, SpaceStats, SpaceUpdate

router = APIRouter(prefix="/spaces", tags=["spaces"])


def _slugify(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


@router.post("", response_model=Space)
async def create_space(
    body: SpaceCreate,
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Space:
    db = get_db()
    settings = request.app.state.settings

    # Resolve tenant_id: admins may override via body, non-admins always use their own.
    if body.tenant_id is not None and body.tenant_id != ctx.tenant_id and not is_admin(ctx):
        raise HTTPException(status_code=400, detail="cannot set tenant_id for another tenant")
    if is_admin(ctx) and body.tenant_id:
        tenant_id = body.tenant_id
    else:
        tenant_id = ctx.tenant_id

    space_id = f"gls_{uuid4().hex[:12]}"
    slug = body.slug or _slugify(body.name)
    now = datetime.now(UTC)
    space = Space(
        id=space_id,
        tenant_id=tenant_id,
        name=body.name,
        slug=slug,
        bucket_uri=f"s3://{settings.minio_bucket}/{space_id}/",
        llm_config=body.llm_config or Space.model_fields["llm_config"].default_factory(),
        stats=SpaceStats(),
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await request.app.state.storage.init_space(
        space_id=space_id,
        schema_markdown=body.schema_markdown,
    )
    return space


@router.get("/{space_id}", response_model=Space)
async def get_space(
    space_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Space:
    db = get_db()
    doc = await db.spaces.find_one(space_query(space_id, ctx))
    if not doc:
        raise HTTPException(status_code=404, detail="space not found")
    return Space.model_validate(doc)


@router.get("", response_model=list[Space])
async def list_spaces(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    tenant_id: str | None = None,
    limit: int = 50,
) -> list[Space]:
    db = get_db()
    query: dict = {}
    if is_admin(ctx):
        if tenant_id:
            query["tenant_id"] = tenant_id
    else:
        query["tenant_id"] = tenant_scope_filter(ctx, tenant_id)
    cursor = db.spaces.find(query).limit(limit)
    return [Space.model_validate(doc) async for doc in cursor]


@router.patch("/{space_id}", response_model=Space)
async def update_space(
    space_id: str,
    body: SpaceUpdate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Space:
    db = get_db()
    update: dict = {"updated_at": datetime.now(UTC)}
    if body.name is not None:
        update["name"] = body.name
    if body.llm_config is not None:
        update["llm_config"] = body.llm_config.model_dump()
    doc = await db.spaces.find_one_and_update(
        space_query(space_id, ctx),
        {"$set": update},
        return_document=True,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="space not found")
    return Space.model_validate(doc)


@router.put("/{space_id}/schema")
async def put_schema(
    space_id: str,
    request: Request,
    schema_markdown: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict:
    db = get_db()
    if not await db.spaces.find_one(space_query(space_id, ctx), {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    await request.app.state.storage.write_page(space_id, "schema.md", schema_markdown)
    return {"ok": True, "path": "schema.md"}


@router.get("/{space_id}/schema")
async def get_schema(
    space_id: str,
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict:
    db = get_db()
    if not await db.spaces.find_one(space_query(space_id, ctx), {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    content = await request.app.state.storage.read_page(space_id, "schema.md")
    return {"path": "schema.md", "content": content}
