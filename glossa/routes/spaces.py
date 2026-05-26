from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from glossa.db.client import get_db
from glossa.models.space import Space, SpaceCreate, SpaceStats, SpaceUpdate

router = APIRouter(prefix="/spaces", tags=["spaces"])


def _slugify(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


@router.post("", response_model=Space)
async def create_space(body: SpaceCreate, request: Request) -> Space:
    db = get_db()
    settings = request.app.state.settings
    space_id = f"gls_{uuid4().hex[:12]}"
    slug = body.slug or _slugify(body.name)
    now = datetime.now(UTC)
    space = Space(
        id=space_id,
        tenant_id=body.tenant_id,
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
async def get_space(space_id: str) -> Space:
    db = get_db()
    doc = await db.spaces.find_one({"id": space_id})
    if not doc:
        raise HTTPException(status_code=404, detail="space not found")
    return Space.model_validate(doc)


@router.get("", response_model=list[Space])
async def list_spaces(tenant_id: str | None = None, limit: int = 50) -> list[Space]:
    db = get_db()
    query = {"tenant_id": tenant_id} if tenant_id else {}
    cursor = db.spaces.find(query).limit(limit)
    return [Space.model_validate(doc) async for doc in cursor]


@router.patch("/{space_id}", response_model=Space)
async def update_space(space_id: str, body: SpaceUpdate) -> Space:
    db = get_db()
    update: dict = {"updated_at": datetime.now(UTC)}
    if body.name is not None:
        update["name"] = body.name
    if body.llm_config is not None:
        update["llm_config"] = body.llm_config.model_dump()
    doc = await db.spaces.find_one_and_update({"id": space_id}, {"$set": update}, return_document=True)
    if not doc:
        raise HTTPException(status_code=404, detail="space not found")
    return Space.model_validate(doc)


@router.put("/{space_id}/schema")
async def put_schema(space_id: str, request: Request, schema_markdown: str) -> dict:
    db = get_db()
    if not await db.spaces.find_one({"id": space_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    await request.app.state.storage.write_page(space_id, "schema.md", schema_markdown)
    return {"ok": True, "path": "schema.md"}


@router.get("/{space_id}/schema")
async def get_schema(space_id: str, request: Request) -> dict:
    db = get_db()
    if not await db.spaces.find_one({"id": space_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    content = await request.app.state.storage.read_page(space_id, "schema.md")
    return {"path": "schema.md", "content": content}
