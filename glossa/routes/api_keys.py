from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from glossa.auth import AuthContext, get_auth_context, is_admin
from glossa.db.client import get_db
from glossa.models.api_key import (
    DEFAULT_SCOPES,
    ApiKey,
    ApiKeyCreate,
    ApiKeyIssued,
    generate_key,
)

router = APIRouter(prefix="/tenants/{tenant_id}/api-keys", tags=["api-keys"])


def _authorize(ctx: AuthContext, tenant_id: str) -> None:
    """Admin or owning-tenant only. 404 (not 403) so existence isn't leaked."""
    if not is_admin(ctx) and ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="tenant not found")


async def _ensure_tenant_exists(tenant_id: str) -> None:
    db = get_db()
    if not await db.tenants.find_one({"id": tenant_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="tenant not found")


@router.post("", response_model=ApiKeyIssued)
async def issue_key(
    tenant_id: str,
    body: ApiKeyCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> ApiKeyIssued:
    _authorize(ctx, tenant_id)
    await _ensure_tenant_exists(tenant_id)
    db = get_db()
    plaintext, prefix, hashed = generate_key()
    api_key = ApiKey(
        id=f"key_{uuid4().hex[:12]}",
        tenant_id=tenant_id,
        hashed_key=hashed,
        prefix=prefix,
        label=body.label,
        scopes=list(body.scopes) if body.scopes is not None else list(DEFAULT_SCOPES),
        created_at=datetime.now(UTC),
    )
    await db.api_keys.insert_one(api_key.model_dump())
    return ApiKeyIssued(api_key=api_key, plaintext=plaintext)


@router.get("", response_model=list[ApiKey])
async def list_keys(
    tenant_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    include_revoked: bool = False,
) -> list[ApiKey]:
    _authorize(ctx, tenant_id)
    await _ensure_tenant_exists(tenant_id)
    db = get_db()
    query: dict = {"tenant_id": tenant_id}
    if not include_revoked:
        query["revoked_at"] = None
    cursor = db.api_keys.find(query)
    return [ApiKey.model_validate(doc) async for doc in cursor]


@router.delete("/{key_id}", response_model=ApiKey)
async def revoke_key(
    tenant_id: str,
    key_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> ApiKey:
    _authorize(ctx, tenant_id)
    db = get_db()
    doc = await db.api_keys.find_one_and_update(
        {"id": key_id, "tenant_id": tenant_id, "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(UTC)}},
        return_document=True,
    )
    if not doc:
        # Either doesn't exist, wrong tenant, or already revoked — disambiguate
        existing = await db.api_keys.find_one({"id": key_id, "tenant_id": tenant_id})
        if not existing:
            raise HTTPException(status_code=404, detail="api key not found")
        return ApiKey.model_validate(existing)  # already revoked, idempotent
    return ApiKey.model_validate(doc)
