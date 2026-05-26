"""Aggregations over RequestEvent for the activity API."""

from datetime import UTC, datetime, timedelta

from glossa.activity.models import RequestActivitySummary, RequestEvent
from glossa.db.client import get_db


def _bounds(hours: int = 24, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(UTC)
    return end - timedelta(hours=hours), end


async def list_recent_events(
    tenant_id: str,
    *,
    method: str | None = None,
    path_prefix: str | None = None,
    status_min: int | None = None,
    limit: int = 100,
) -> list[RequestEvent]:
    db = get_db()
    query: dict = {"tenant_id": tenant_id}
    if method:
        query["method"] = method.upper()
    if path_prefix:
        query["path"] = {"$regex": f"^{path_prefix}"}
    if status_min is not None:
        query["status_code"] = {"$gte": status_min}
    cursor = db.request_events.find(query).sort("created_at", -1).limit(limit)
    return [RequestEvent.model_validate(doc) async for doc in cursor]


async def summarize(tenant_id: str, *, hours: int = 24) -> RequestActivitySummary:
    db = get_db()
    start, end = _bounds(hours)
    pipeline = [
        {"$match": {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": None,
                "request_count": {"$sum": 1},
                "error_count": {"$sum": {"$cond": [{"$gte": ["$status_code", 500]}, 1, 0]}},
                "total_duration_ms": {"$sum": "$duration_ms"},
            }
        },
    ]
    totals = [doc async for doc in db.request_events.aggregate(pipeline)]
    base = totals[0] if totals else {}
    by_status = await _group_by(db, tenant_id, start, end, "status_code")
    by_path = await _group_by(db, tenant_id, start, end, "path")

    return RequestActivitySummary(
        tenant_id=tenant_id,
        period_start=start,
        period_end=end,
        request_count=base.get("request_count", 0),
        error_count=base.get("error_count", 0),
        avg_duration_ms=(base["total_duration_ms"] / base["request_count"] if base.get("request_count") else 0.0),
        by_status={str(k): v for k, v in by_status.items()},
        by_path=by_path,
    )


async def _group_by(db, tenant_id: str, start, end, field: str) -> dict:
    pipeline = [
        {"$match": {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    return {str(doc["_id"]): doc["count"] async for doc in db.request_events.aggregate(pipeline)}
