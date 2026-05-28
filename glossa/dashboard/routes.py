"""HTML dashboard routes. Renders via Jinja2 + HTMX + Pico.css.

Sibling to /auth/* (OAuth flow). All routes here live under /dashboard/.
Static-asset routes are not registered here; we use CDN for HTMX + Pico.

Authentication:
- /dashboard/login is always public (renders provider buttons).
- All other /dashboard/* require a session. Unauthenticated visits
  redirect to /dashboard/login via require_session's content-negotiated
  behavior.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from glossa.db.client import get_db
from glossa.models.membership import TenantMember
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
