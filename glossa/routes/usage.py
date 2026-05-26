from fastapi import APIRouter, HTTPException

from glossa.usage.aggregator import (
    aggregate_period,
    aggregate_recent_events,
    aggregate_tenant_by_space,
    aggregate_tenant_summary,
)
from glossa.usage.models import (
    QuotaStatus,
    TenantQuota,
    TenantQuotaUpdate,
    UsageEvent,
    UsagePeriodSummary,
)
from glossa.usage.quota import get_quota, get_quota_status, upsert_quota

tenant_router = APIRouter(prefix="/tenants/{tenant_id}", tags=["usage"])
space_router = APIRouter(prefix="/spaces/{space_id}", tags=["usage"])


@tenant_router.get("/usage", response_model=UsagePeriodSummary)
async def get_tenant_usage(tenant_id: str, period: str | None = None) -> UsagePeriodSummary:
    """Per-period rollup (default: current calendar month)."""
    return await aggregate_period(tenant_id, period)


@tenant_router.get("/usage/summary")
async def get_tenant_usage_summary(tenant_id: str) -> dict:
    """All-time totals for the tenant. Admin / debugging surface."""
    return await aggregate_tenant_summary(tenant_id)


@tenant_router.get("/usage/by-space")
async def get_tenant_usage_by_space(tenant_id: str, period: str | None = None) -> list[dict]:
    return await aggregate_tenant_by_space(tenant_id, period)


@tenant_router.get("/usage/events", response_model=list[UsageEvent])
async def list_tenant_usage_events(
    tenant_id: str,
    space_id: str | None = None,
    limit: int = 50,
) -> list[UsageEvent]:
    return await aggregate_recent_events(tenant_id, space_id=space_id, limit=limit)


@tenant_router.get("/quota", response_model=QuotaStatus)
async def get_tenant_quota_status(tenant_id: str) -> QuotaStatus:
    """Current quota status: usage, limit, remaining, blocked. Safe to poll."""
    return await get_quota_status(tenant_id)


@tenant_router.get("/quota/config", response_model=TenantQuota | None)
async def get_tenant_quota_config(tenant_id: str) -> TenantQuota | None:
    """Raw quota config row. Returns ``null`` for unlimited tenants."""
    return await get_quota(tenant_id)


@tenant_router.put("/quota", response_model=TenantQuota)
async def update_tenant_quota(tenant_id: str, body: TenantQuotaUpdate) -> TenantQuota:
    return await upsert_quota(
        tenant_id=tenant_id,
        monthly_cost_limit_usd=body.monthly_cost_limit_usd,
        monthly_token_limit=body.monthly_token_limit,
        allowed_models=body.allowed_models,
        notes=body.notes,
    )


@space_router.get("/usage/events", response_model=list[UsageEvent])
async def list_space_usage_events(space_id: str, limit: int = 50) -> list[UsageEvent]:
    """Newest-first events for one space. Tenant is inferred from the events."""
    from glossa.db.client import get_db

    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id}, {"tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    return await aggregate_recent_events(space_doc["tenant_id"], space_id=space_id, limit=limit)
