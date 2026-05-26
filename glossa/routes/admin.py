from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pymongo.errors import DuplicateKeyError

from glossa.auth import AuthContext, require_scope
from glossa.db.client import get_db
from glossa.models.api_key import Scope
from glossa.models.tenant import Tenant, TenantCreate, TenantPlan, TenantStatus, TenantUpdate

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/tenants", response_model=Tenant)
async def create_tenant(
    body: TenantCreate,
    ctx: Annotated[AuthContext, Depends(require_scope(Scope.ADMIN))],
) -> Tenant:
    db = get_db()
    now = datetime.now(UTC)
    tenant = Tenant(
        id=f"tnt_{uuid4().hex[:12]}",
        name=body.name,
        owner_email=body.owner_email,
        plan=body.plan or TenantPlan.FREE,
        status=TenantStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    try:
        await db.tenants.insert_one(tenant.model_dump())
    except DuplicateKeyError as e:
        raise HTTPException(status_code=409, detail="owner_email already in use") from e
    return tenant


@router.get("/tenants", response_model=list[Tenant])
async def list_tenants(
    ctx: Annotated[AuthContext, Depends(require_scope(Scope.ADMIN))],
    status: TenantStatus | None = None,
    limit: int = 100,
) -> list[Tenant]:
    db = get_db()
    query: dict = {}
    if status:
        query["status"] = status.value
    cursor = db.tenants.find(query).limit(limit)
    return [Tenant.model_validate(doc) async for doc in cursor]


@router.get("/tenants/{tenant_id}", response_model=Tenant)
async def get_tenant(
    tenant_id: str,
    ctx: Annotated[AuthContext, Depends(require_scope(Scope.ADMIN))],
) -> Tenant:
    db = get_db()
    doc = await db.tenants.find_one({"id": tenant_id})
    if not doc:
        raise HTTPException(status_code=404, detail="tenant not found")
    return Tenant.model_validate(doc)


@router.patch("/tenants/{tenant_id}", response_model=Tenant)
async def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    ctx: Annotated[AuthContext, Depends(require_scope(Scope.ADMIN))],
) -> Tenant:
    db = get_db()
    update: dict = {"updated_at": datetime.now(UTC)}
    for field in ("name", "owner_email", "plan", "status"):
        value = getattr(body, field)
        if value is not None:
            update[field] = value.value if hasattr(value, "value") else value
    try:
        doc = await db.tenants.find_one_and_update(
            {"id": tenant_id},
            {"$set": update},
            return_document=True,
        )
    except DuplicateKeyError as e:
        raise HTTPException(status_code=409, detail="owner_email already in use") from e
    if not doc:
        raise HTTPException(status_code=404, detail="tenant not found")
    return Tenant.model_validate(doc)
