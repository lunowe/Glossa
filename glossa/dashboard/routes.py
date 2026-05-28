"""HTML dashboard routes. Renders via Jinja2 + HTMX + Pico.css.

Sibling to /auth/* (OAuth flow). All routes here live under /dashboard/.
Static-asset routes are not registered here; we use CDN for HTMX + Pico.

Authentication:
- /dashboard/login is always public (renders provider buttons).
- All other /dashboard/* require a session. Unauthenticated visits
  redirect to /dashboard/login via require_session's content-negotiated
  behavior.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from glossa.dashboard.access import (
    count_owners,
    require_admin_membership,
    require_membership,
)
from glossa.db.client import get_db
from glossa.models.membership import (
    Invite,
    TenantMember,
    TenantRole,
)
from glossa.models.tenant import Tenant
from glossa.sessions import SessionContext, get_session_user, require_session

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    request: Request,
    ctx: Annotated[SessionContext | None, Depends(get_session_user)],
) -> HTMLResponse | RedirectResponse:
    if ctx is not None:
        return RedirectResponse(url="/dashboard/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@router.get("", response_class=HTMLResponse, response_model=None)
async def dashboard_index(
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> HTMLResponse | RedirectResponse:
    db = get_db()
    memberships = [TenantMember.model_validate(doc) async for doc in db.tenant_members.find({"user_id": ctx.user.id})]
    tenant_ids = [m.tenant_id for m in memberships]
    tenants = [Tenant.model_validate(doc) async for doc in db.tenants.find({"id": {"$in": tenant_ids}})]
    # Pair each membership with its tenant for the template
    by_id = {t.id: t for t in tenants}
    rows = [{"tenant": by_id[m.tenant_id], "role": m.role} for m in memberships if m.tenant_id in by_id]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"user": ctx.user, "rows": rows},
    )


# --- Tenant-scoped routes ------------------------------------------------------


@router.get("/t/{tenant_id}/", response_class=HTMLResponse, include_in_schema=False)
async def tenant_overview(
    tenant_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> HTMLResponse:
    member = await require_membership(tenant_id, ctx)
    db = get_db()
    tenant = Tenant.model_validate(await db.tenants.find_one({"id": tenant_id}))
    return templates.TemplateResponse(
        request,
        "tenant_overview.html",
        {
            "user": ctx.user,
            "tenant": tenant,
            "current_role": member.role,
            "current_tenant_id": tenant_id,
        },
    )


@router.get("/t/{tenant_id}/members", response_class=HTMLResponse, include_in_schema=False)
async def tenant_members(
    tenant_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> HTMLResponse:
    my_member = await require_membership(tenant_id, ctx)
    db = get_db()
    tenant = Tenant.model_validate(await db.tenants.find_one({"id": tenant_id}))
    members = [TenantMember.model_validate(doc) async for doc in db.tenant_members.find({"tenant_id": tenant_id})]
    user_ids = [m.user_id for m in members]
    user_docs = {doc["id"]: doc async for doc in db.users.find({"id": {"$in": user_ids}})}
    rows = [
        {
            "member": m,
            "user": user_docs.get(m.user_id, {}),
            "is_self": m.user_id == ctx.user.id,
        }
        for m in members
    ]
    return templates.TemplateResponse(
        request,
        "tenant_members.html",
        {
            "user": ctx.user,
            "tenant": tenant,
            "rows": rows,
            "can_manage": my_member.role in (TenantRole.OWNER, TenantRole.ADMIN),
            "current_tenant_id": tenant_id,
            "current_role": my_member.role,
        },
    )


@router.post(
    "/t/{tenant_id}/members/{member_id}/role",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def change_member_role(
    tenant_id: str,
    member_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> RedirectResponse:
    await require_admin_membership(tenant_id, ctx)
    form = await request.form()
    new_role_raw = form.get("role")
    if new_role_raw is None:
        raise HTTPException(status_code=400, detail="missing role")
    try:
        new_role = TenantRole(new_role_raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid role") from e
    db = get_db()
    target = await db.tenant_members.find_one({"id": member_id, "tenant_id": tenant_id})
    if not target:
        raise HTTPException(status_code=404, detail="member not found")
    # Don't allow demoting the last owner
    if target["role"] == TenantRole.OWNER.value and new_role != TenantRole.OWNER and await count_owners(tenant_id) <= 1:
        raise HTTPException(status_code=400, detail="cannot demote the sole owner")
    await db.tenant_members.update_one(
        {"id": member_id, "tenant_id": tenant_id},
        {"$set": {"role": new_role.value}},
    )
    return RedirectResponse(url=f"/dashboard/t/{tenant_id}/members", status_code=303)


@router.post(
    "/t/{tenant_id}/members/{member_id}/remove",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def remove_member(
    tenant_id: str,
    member_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> RedirectResponse:
    await require_admin_membership(tenant_id, ctx)
    db = get_db()
    target = await db.tenant_members.find_one({"id": member_id, "tenant_id": tenant_id})
    if not target:
        raise HTTPException(status_code=404, detail="member not found")
    if target["role"] == TenantRole.OWNER.value and await count_owners(tenant_id) <= 1:
        raise HTTPException(status_code=400, detail="cannot remove the sole owner")
    await db.tenant_members.delete_one({"id": member_id, "tenant_id": tenant_id})
    return RedirectResponse(url=f"/dashboard/t/{tenant_id}/members", status_code=303)


@router.get("/t/{tenant_id}/invites", response_class=HTMLResponse, include_in_schema=False)
async def tenant_invites(
    tenant_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> HTMLResponse:
    my_member = await require_membership(tenant_id, ctx)
    db = get_db()
    tenant = Tenant.model_validate(await db.tenants.find_one({"id": tenant_id}))
    # Active invites: not revoked, not accepted, not expired
    now = datetime.now(UTC)
    pending = [
        Invite.model_validate(doc)
        async for doc in db.invites.find(
            {
                "tenant_id": tenant_id,
                "revoked_at": None,
                "accepted_at": None,
                "expires_at": {"$gt": now},
            }
        )
    ]
    base_url = request.app.state.settings.base_url.rstrip("/")
    return templates.TemplateResponse(
        request,
        "tenant_invites.html",
        {
            "user": ctx.user,
            "tenant": tenant,
            "invites": pending,
            "base_url": base_url,
            "can_manage": my_member.role in (TenantRole.OWNER, TenantRole.ADMIN),
            "current_tenant_id": tenant_id,
            "current_role": my_member.role,
        },
    )


@router.post("/t/{tenant_id}/invites", response_class=HTMLResponse, include_in_schema=False)
async def create_invite(
    tenant_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> RedirectResponse:
    await require_admin_membership(tenant_id, ctx)
    form = await request.form()
    role_raw = form.get("role") or TenantRole.MEMBER.value
    try:
        role = TenantRole(role_raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid role") from e
    ttl_hours_raw = form.get("ttl_hours") or "168"
    try:
        ttl_hours = max(1, min(int(ttl_hours_raw), 720))  # clamp 1h-30d
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid ttl_hours") from e

    now = datetime.now(UTC)
    invite = Invite(
        id=f"inv_{uuid4().hex[:12]}",
        tenant_id=tenant_id,
        token=token_urlsafe(32),
        role=role,
        created_by_user_id=ctx.user.id,
        created_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )
    await get_db().invites.insert_one(invite.model_dump())
    return RedirectResponse(url=f"/dashboard/t/{tenant_id}/invites", status_code=303)


@router.post(
    "/t/{tenant_id}/invites/{invite_id}/revoke",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def revoke_invite(
    tenant_id: str,
    invite_id: str,
    request: Request,
    ctx: Annotated[SessionContext, Depends(require_session)],
) -> RedirectResponse:
    await require_admin_membership(tenant_id, ctx)
    await get_db().invites.update_one(
        {"id": invite_id, "tenant_id": tenant_id, "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(UTC)}},
    )
    return RedirectResponse(url=f"/dashboard/t/{tenant_id}/invites", status_code=303)


@router.get(
    "/invites/accept/{token}",
    response_class=HTMLResponse,
    include_in_schema=False,
    response_model=None,
)
async def invite_accept(
    token: str,
    request: Request,
    ctx: Annotated[SessionContext | None, Depends(get_session_user)],
) -> HTMLResponse | RedirectResponse:
    db = get_db()
    invite_doc = await db.invites.find_one({"token": token})
    if not invite_doc:
        return templates.TemplateResponse(
            request,
            "invite_invalid.html",
            {"reason": "Invite not found."},
            status_code=404,
        )
    invite = Invite.model_validate(invite_doc)
    now = datetime.now(UTC)
    if invite.revoked_at is not None:
        return templates.TemplateResponse(
            request,
            "invite_invalid.html",
            {"reason": "This invite was revoked."},
            status_code=410,
        )
    if invite.accepted_at is not None:
        return templates.TemplateResponse(
            request,
            "invite_invalid.html",
            {"reason": "This invite has already been accepted."},
            status_code=410,
        )
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return templates.TemplateResponse(
            request,
            "invite_invalid.html",
            {"reason": "This invite has expired."},
            status_code=410,
        )

    if ctx is None:
        return templates.TemplateResponse(
            request,
            "invite_sign_in.html",
            {
                "next": f"/dashboard/invites/accept/{token}",
                "invite_role": invite.role.value,
            },
        )

    # Logged-in: accept the invite. If the user is already a member, just route them to the tenant.
    existing = await db.tenant_members.find_one({"tenant_id": invite.tenant_id, "user_id": ctx.user.id})
    if existing is None:
        await db.tenant_members.insert_one(
            TenantMember(
                id=f"mem_{uuid4().hex[:12]}",
                tenant_id=invite.tenant_id,
                user_id=ctx.user.id,
                role=invite.role,
                joined_at=now,
            ).model_dump()
        )
    await db.invites.update_one({"id": invite.id}, {"$set": {"accepted_at": now}})
    return RedirectResponse(url=f"/dashboard/t/{invite.tenant_id}/", status_code=303)
