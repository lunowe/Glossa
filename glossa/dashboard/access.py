"""Per-tenant authorization helpers for the dashboard."""

from fastapi import HTTPException

from glossa.db.client import get_db
from glossa.models.membership import TenantMember, TenantRole
from glossa.sessions import SessionContext


async def get_membership(tenant_id: str, user_id: str) -> TenantMember | None:
    db = get_db()
    doc = await db.tenant_members.find_one({"tenant_id": tenant_id, "user_id": user_id})
    return TenantMember.model_validate(doc) if doc else None


async def require_membership(tenant_id: str, ctx: SessionContext) -> TenantMember:
    """Membership lookup; 404 if the user isn't in this tenant (avoid leaking existence)."""
    member = await get_membership(tenant_id, ctx.user.id)
    if member is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return member


async def require_admin_membership(tenant_id: str, ctx: SessionContext) -> TenantMember:
    member = await require_membership(tenant_id, ctx)
    if member.role not in (TenantRole.OWNER, TenantRole.ADMIN):
        raise HTTPException(status_code=403, detail="admin or owner role required")
    return member


async def count_owners(tenant_id: str) -> int:
    db = get_db()
    return await db.tenant_members.count_documents({"tenant_id": tenant_id, "role": TenantRole.OWNER.value})
