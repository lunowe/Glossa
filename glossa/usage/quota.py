"""Per-tenant quota enforcement.

The quota row at ``glossa.tenant_quotas`` holds six independent dimensions:

  * ``monthly_cost_limit_usd`` — USD ceiling for the current calendar month
  * ``monthly_token_limit``    — token ceiling for the current calendar month
  * ``max_sources_per_space``  — count of sources per individual space
  * ``max_storage_bytes``      — total markdown bytes across all spaces
  * ``max_requests_per_minute``— sliding-window rate limit (in-process)
  * ``allowed_models``         — model allow-list (enforced elsewhere)

Any field set to ``None`` (or missing) means "no limit" on that dimension.

Call sites:

  * ``check_quota(tenant_id)`` runs the *broad* checks (cost, tokens, rate)
    before any billable LLM operation. It is called from ingest, query, and
    lint routes. Raises ``QuotaExceededError`` on block (routes translate to
    HTTP 402).
  * ``check_source_quota(tenant_id, space_id)`` runs the per-space source
    count check at source-creation time. Called from ``routes/sources.py``.
  * ``check_storage_quota_before_write(tenant_id, new_bytes)`` runs the
    storage-bytes check before any markdown is persisted. Called from
    ``ingest/page_writer.py`` and from the schema-write route in
    ``routes/spaces.py``.
"""

from datetime import UTC, datetime

from glossa.db.client import get_db
from glossa.usage.aggregator import aggregate_period, period_for
from glossa.usage.models import QuotaStatus, TenantQuota
from glossa.usage.rate_limit import get_rate_limiter


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
    max_sources_per_space: int | None = None,
    max_storage_bytes: int | None = None,
    max_requests_per_minute: int | None = None,
    notes: str | None = None,
) -> TenantQuota:
    db = get_db()
    quota = TenantQuota(
        tenant_id=tenant_id,
        monthly_cost_limit_usd=monthly_cost_limit_usd,
        monthly_token_limit=monthly_token_limit,
        allowed_models=allowed_models,
        max_sources_per_space=max_sources_per_space,
        max_storage_bytes=max_storage_bytes,
        max_requests_per_minute=max_requests_per_minute,
        notes=notes,
        updated_at=datetime.now(UTC),
    )
    await db.tenant_quotas.update_one(
        {"tenant_id": tenant_id},
        {"$set": quota.model_dump()},
        upsert=True,
    )
    return quota


async def _tenant_space_ids(tenant_id: str) -> list[str]:
    db = get_db()
    cursor = db.spaces.find({"tenant_id": tenant_id}, {"id": 1})
    return [doc["id"] async for doc in cursor]


async def _storage_bytes_used(tenant_id: str) -> int:
    """Sum of ``pages.size_bytes`` across all spaces for this tenant.

    Two-step query: gather the tenant's space ids, then aggregate over pages.
    Cheap at our volume; if it ever becomes hot, store a per-space rollup.
    """
    db = get_db()
    space_ids = await _tenant_space_ids(tenant_id)
    if not space_ids:
        return 0
    pipeline = [
        {"$match": {"space_id": {"$in": space_ids}}},
        {"$group": {"_id": None, "total": {"$sum": "$size_bytes"}}},
    ]
    docs = [doc async for doc in db.pages.aggregate(pipeline)]
    if not docs:
        return 0
    value = docs[0].get("total")
    return int(value) if value is not None else 0


async def _max_sources_per_space(tenant_id: str) -> int:
    """Highest current source count across this tenant's spaces (0 if none)."""
    db = get_db()
    space_ids = await _tenant_space_ids(tenant_id)
    if not space_ids:
        return 0
    pipeline = [
        {"$match": {"space_id": {"$in": space_ids}}},
        {"$group": {"_id": "$space_id", "count": {"$sum": 1}}},
        {"$group": {"_id": None, "max_count": {"$max": "$count"}}},
    ]
    docs = [doc async for doc in db.sources.aggregate(pipeline)]
    if not docs:
        return 0
    value = docs[0].get("max_count")
    return int(value) if value is not None else 0


async def get_quota_status(tenant_id: str, period: str | None = None) -> QuotaStatus:
    period = period or period_for(datetime.now(UTC))
    quota = await get_quota(tenant_id)
    summary = await aggregate_period(tenant_id, period)

    cost_limit = quota.monthly_cost_limit_usd if quota else None
    token_limit = quota.monthly_token_limit if quota else None
    sources_per_space_max = quota.max_sources_per_space if quota else None
    storage_bytes_limit = quota.max_storage_bytes if quota else None
    rate_limit = quota.max_requests_per_minute if quota else None

    cost_used = summary.cost_usd
    token_used = summary.total_tokens
    storage_bytes_used = await _storage_bytes_used(tenant_id)
    sources_per_space_used_max = await _max_sources_per_space(tenant_id)
    requests_per_minute_used = await get_rate_limiter().current_count(tenant_id)

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
        sources_per_space_max=sources_per_space_max,
        sources_per_space_used_max=sources_per_space_used_max,
        storage_bytes_used=storage_bytes_used,
        storage_bytes_limit=storage_bytes_limit,
        requests_per_minute_limit=rate_limit,
        requests_per_minute_used=requests_per_minute_used,
        blocked=blocked,
    )


async def check_quota(tenant_id: str) -> QuotaStatus:
    """Run the broad quota checks before a billable operation.

    Order of checks: cost limit, token limit, rate limit. First block wins.
    Per-space (source) and per-write (storage) quotas are enforced at their
    specific operation sites; see ``check_source_quota`` and
    ``check_storage_quota_before_write``.
    """
    status = await get_quota_status(tenant_id)
    if status.cost_usd_limit is not None and status.cost_usd_used >= status.cost_usd_limit:
        reason = (
            f"Monthly cost limit reached: ${status.cost_usd_used:.4f} "
            f"of ${status.cost_usd_limit:.2f} for {status.period}"
        )
        raise QuotaExceededError(status, reason)
    if status.token_limit is not None and status.token_used >= status.token_limit:
        reason = f"Monthly token limit reached: {status.token_used} of {status.token_limit} for {status.period}"
        raise QuotaExceededError(status, reason)
    if status.requests_per_minute_limit is not None:
        allowed, current = await get_rate_limiter().check_and_record(tenant_id, status.requests_per_minute_limit)
        if not allowed:
            reason = f"Rate limit reached: {current} of {status.requests_per_minute_limit} requests per minute"
            # Refresh the count we report in the status object.
            blocked_status = status.model_copy(update={"requests_per_minute_used": current, "blocked": True})
            raise QuotaExceededError(blocked_status, reason)
    return status


async def check_source_quota(tenant_id: str, space_id: str) -> None:
    """Raise if creating one more source in ``space_id`` would exceed quota.

    The limit applies per-space; sources in *other* spaces of the same tenant
    do not count toward this individual space's ceiling.
    """
    quota = await get_quota(tenant_id)
    if not quota or quota.max_sources_per_space is None:
        return
    db = get_db()
    current = await db.sources.count_documents({"space_id": space_id})
    if current >= quota.max_sources_per_space:
        status = await get_quota_status(tenant_id)
        reason = f"Source count limit reached: {current} of {quota.max_sources_per_space} sources in space {space_id}"
        raise QuotaExceededError(status, reason)


async def check_storage_quota_before_write(tenant_id: str, new_bytes: int) -> None:
    """Raise if writing ``new_bytes`` bytes would push the tenant over quota.

    ``new_bytes`` is the byte length of the upcoming markdown content. We sum
    that against current usage; this is a slight over-estimate when the write
    is replacing an existing page (the old bytes will be subtracted when the
    row is updated). Accepting the conservatism — keeps the check simple.
    """
    quota = await get_quota(tenant_id)
    if not quota or quota.max_storage_bytes is None:
        return
    current = await _storage_bytes_used(tenant_id)
    if current + new_bytes > quota.max_storage_bytes:
        status = await get_quota_status(tenant_id)
        reason = f"Storage bytes limit reached: {current + new_bytes} > {quota.max_storage_bytes} bytes"
        raise QuotaExceededError(status, reason)
