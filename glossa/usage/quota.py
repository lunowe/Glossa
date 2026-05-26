"""Per-tenant quota enforcement.

For v0.1, two limits per tenant: ``monthly_cost_limit_usd`` and
``monthly_token_limit``. Either or both can be set. ``None`` means unlimited.
Both check against the rolling current calendar month; resets at month boundary.

Call ``check_quota(tenant_id)`` before any billable LLM operation. Raises
``QuotaExceededError`` on block, which routes should translate into HTTP 402.
"""

from datetime import UTC, datetime

from glossa.db.client import get_db
from glossa.usage.aggregator import aggregate_period, period_for
from glossa.usage.models import QuotaStatus, TenantQuota


class QuotaExceededError(Exception):
    def __init__(self, status: QuotaStatus, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


async def get_quota(tenant_id: str) -> TenantQuota | None:
    """Read the tenant's quota document. ``None`` means no limits configured."""
    db = get_db()
    doc = await db.tenant_quotas.find_one({"tenant_id": tenant_id})
    if not doc:
        return None
    return TenantQuota.model_validate(doc)


async def upsert_quota(
    *,
    tenant_id: str,
    monthly_cost_limit_usd: float | None = None,
    monthly_token_limit: int | None = None,
    allowed_models: list[str] | None = None,
    notes: str | None = None,
) -> TenantQuota:
    db = get_db()
    quota = TenantQuota(
        tenant_id=tenant_id,
        monthly_cost_limit_usd=monthly_cost_limit_usd,
        monthly_token_limit=monthly_token_limit,
        allowed_models=allowed_models,
        notes=notes,
        updated_at=datetime.now(UTC),
    )
    await db.tenant_quotas.update_one(
        {"tenant_id": tenant_id},
        {"$set": quota.model_dump()},
        upsert=True,
    )
    return quota


async def get_quota_status(tenant_id: str, period: str | None = None) -> QuotaStatus:
    period = period or period_for(datetime.now(UTC))
    quota = await get_quota(tenant_id)
    summary = await aggregate_period(tenant_id, period)

    cost_limit = quota.monthly_cost_limit_usd if quota else None
    token_limit = quota.monthly_token_limit if quota else None
    cost_used = summary.cost_usd
    token_used = summary.total_tokens

    cost_remaining = None if cost_limit is None else max(0.0, round(cost_limit - cost_used, 6))
    token_remaining = None if token_limit is None else max(0, token_limit - token_used)
    blocked = (cost_limit is not None and cost_used >= cost_limit) or (
        token_limit is not None and token_used >= token_limit
    )

    return QuotaStatus(
        tenant_id=tenant_id,
        period=period,
        cost_usd_used=cost_used,
        cost_usd_limit=cost_limit,
        cost_usd_remaining=cost_remaining,
        token_used=token_used,
        token_limit=token_limit,
        token_remaining=token_remaining,
        blocked=blocked,
    )


async def check_quota(tenant_id: str) -> QuotaStatus:
    """Raise ``QuotaExceededError`` if the tenant is over their monthly limit."""
    status = await get_quota_status(tenant_id)
    if status.blocked:
        if status.cost_usd_limit is not None and status.cost_usd_used >= status.cost_usd_limit:
            reason = (
                f"Monthly cost limit reached: ${status.cost_usd_used:.4f} "
                f"of ${status.cost_usd_limit:.2f} for {status.period}"
            )
        else:
            reason = f"Monthly token limit reached: {status.token_used} of {status.token_limit} for {status.period}"
        raise QuotaExceededError(status, reason)
    return status
