from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from glossa.activity.aggregator import list_recent_events, summarize
from glossa.activity.models import RequestActivitySummary, RequestEvent
from glossa.auth import AuthContext, get_auth_context, is_admin

router = APIRouter(prefix="/tenants/{tenant_id}/activity", tags=["activity"])


def _authorize(ctx: AuthContext, tenant_id: str) -> None:
    if not is_admin(ctx) and ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="tenant not found")


@router.get("/requests", response_model=list[RequestEvent])
async def list_requests(
    tenant_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    method: str | None = None,
    path_prefix: str | None = None,
    status_min: int | None = None,
    limit: int = 100,
) -> list[RequestEvent]:
    _authorize(ctx, tenant_id)
    return await list_recent_events(
        tenant_id,
        method=method,
        path_prefix=path_prefix,
        status_min=status_min,
        limit=limit,
    )


@router.get("/summary", response_model=RequestActivitySummary)
async def get_summary(
    tenant_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    hours: int = 24,
) -> RequestActivitySummary:
    _authorize(ctx, tenant_id)
    return await summarize(tenant_id, hours=hours)
