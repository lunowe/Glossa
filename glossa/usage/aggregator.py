"""Aggregate queries over UsageEvent.

Mongo aggregation pipelines, computed on the fly. At low-to-moderate event
volume (~10k events/month/tenant) this is fine; if it ever becomes hot we
can layer in a pre-computed period rollup collection without changing the
public API.
"""

from datetime import UTC, datetime

from glossa.db.client import get_db
from glossa.usage.models import UsageEvent, UsagePeriodSummary


def period_for(dt: datetime) -> str:
    """Calendar-month period label, e.g. ``2026-05``."""
    return dt.astimezone(UTC).strftime("%Y-%m")


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    """Inclusive start, exclusive end for a ``YYYY-MM`` period."""
    year, month = int(period[:4]), int(period[5:7])
    start = datetime(year, month, 1, tzinfo=UTC)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(end_year, end_month, 1, tzinfo=UTC)
    return start, end


async def aggregate_period(tenant_id: str, period: str | None = None) -> UsagePeriodSummary:
    """Roll up one tenant's usage for one calendar month."""
    period = period or period_for(datetime.now(UTC))
    start, end = _period_bounds(period)
    db = get_db()

    pipeline = [
        {"$match": {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": None,
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "cache_creation_input_tokens": {"$sum": "$cache_creation_input_tokens"},
                "cache_read_input_tokens": {"$sum": "$cache_read_input_tokens"},
                "cost_usd": {"$sum": "$cost_usd"},
                "event_count": {"$sum": 1},
            }
        },
    ]
    totals = [doc async for doc in db.usage_events.aggregate(pipeline)]
    base = totals[0] if totals else {}

    by_operation = await _group_by(db, tenant_id, start, end, "operation")
    by_model = await _group_by(db, tenant_id, start, end, "model")

    return UsagePeriodSummary(
        tenant_id=tenant_id,
        period=period,
        input_tokens=base.get("input_tokens", 0),
        output_tokens=base.get("output_tokens", 0),
        cache_creation_input_tokens=base.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=base.get("cache_read_input_tokens", 0),
        total_tokens=(
            base.get("input_tokens", 0)
            + base.get("output_tokens", 0)
            + base.get("cache_creation_input_tokens", 0)
            + base.get("cache_read_input_tokens", 0)
        ),
        cost_usd=round(base.get("cost_usd", 0.0), 6),
        event_count=base.get("event_count", 0),
        by_operation=by_operation,
        by_model=by_model,
    )


async def _group_by(db, tenant_id: str, start: datetime, end: datetime, field: str) -> dict[str, dict]:
    pipeline = [
        {"$match": {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": f"${field}",
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "cache_creation_input_tokens": {"$sum": "$cache_creation_input_tokens"},
                "cache_read_input_tokens": {"$sum": "$cache_read_input_tokens"},
                "cost_usd": {"$sum": "$cost_usd"},
                "event_count": {"$sum": 1},
            }
        },
    ]
    return {
        doc["_id"]: {
            "input_tokens": doc["input_tokens"],
            "output_tokens": doc["output_tokens"],
            "cache_creation_input_tokens": doc["cache_creation_input_tokens"],
            "cache_read_input_tokens": doc["cache_read_input_tokens"],
            "cost_usd": round(doc["cost_usd"], 6),
            "event_count": doc["event_count"],
        }
        async for doc in db.usage_events.aggregate(pipeline)
        if doc["_id"]
    }


async def aggregate_tenant_summary(tenant_id: str) -> dict:
    """All-time totals for a tenant. Useful for admin dashboards."""
    db = get_db()
    pipeline = [
        {"$match": {"tenant_id": tenant_id}},
        {
            "$group": {
                "_id": None,
                "total_cost_usd": {"$sum": "$cost_usd"},
                "total_tokens": {
                    "$sum": {
                        "$add": [
                            "$input_tokens",
                            "$output_tokens",
                            "$cache_creation_input_tokens",
                            "$cache_read_input_tokens",
                        ]
                    }
                },
                "event_count": {"$sum": 1},
                "first_event": {"$min": "$created_at"},
                "last_event": {"$max": "$created_at"},
            }
        },
    ]
    docs = [doc async for doc in db.usage_events.aggregate(pipeline)]
    if not docs:
        return {
            "tenant_id": tenant_id,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "event_count": 0,
            "first_event": None,
            "last_event": None,
        }
    d = docs[0]
    return {
        "tenant_id": tenant_id,
        "total_cost_usd": round(d["total_cost_usd"], 6),
        "total_tokens": d["total_tokens"],
        "event_count": d["event_count"],
        "first_event": d["first_event"],
        "last_event": d["last_event"],
    }


async def aggregate_tenant_by_space(tenant_id: str, period: str | None = None) -> list[dict]:
    """Per-space breakdown for the given period (default: current month)."""
    period = period or period_for(datetime.now(UTC))
    start, end = _period_bounds(period)
    db = get_db()
    pipeline = [
        {"$match": {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": "$space_id",
                "cost_usd": {"$sum": "$cost_usd"},
                "total_tokens": {
                    "$sum": {
                        "$add": [
                            "$input_tokens",
                            "$output_tokens",
                            "$cache_creation_input_tokens",
                            "$cache_read_input_tokens",
                        ]
                    }
                },
                "event_count": {"$sum": 1},
            }
        },
        {"$sort": {"cost_usd": -1}},
    ]
    return [
        {
            "space_id": doc["_id"],
            "cost_usd": round(doc["cost_usd"], 6),
            "total_tokens": doc["total_tokens"],
            "event_count": doc["event_count"],
        }
        async for doc in db.usage_events.aggregate(pipeline)
    ]


async def aggregate_recent_events(
    tenant_id: str,
    *,
    space_id: str | None = None,
    limit: int = 50,
) -> list[UsageEvent]:
    """Paginated event list. Newest first."""
    db = get_db()
    query: dict = {"tenant_id": tenant_id}
    if space_id:
        query["space_id"] = space_id
    cursor = db.usage_events.find(query).sort("created_at", -1).limit(limit)
    return [UsageEvent.model_validate(doc) async for doc in cursor]
