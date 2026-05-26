"""Record one UsageEvent per LLM call.

Hooked from every place that calls ``llm.chat(...)``. Normalizes the provider's
usage shape into the Anthropic-style fields we store, then computes cost using
``glossa.pricing.compute_cost_usd``.

Recording failures never propagate to the caller — billing/analytics tracking
must not break an ingest or query. Failures are logged.
"""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from glossa.db.client import get_db
from glossa.pricing import compute_cost_usd
from glossa.usage.models import Operation, UsageEvent

logger = logging.getLogger(__name__)


def _normalize_usage(raw: dict | None) -> dict:
    """Normalize OpenAI and Anthropic usage dicts to the same shape.

    OpenAI uses ``prompt_tokens`` / ``completion_tokens``; Anthropic uses
    ``input_tokens`` / ``output_tokens`` plus cache split fields. Any missing
    field defaults to 0.
    """
    raw = raw or {}
    return {
        "input_tokens": int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or raw.get("completion_tokens") or 0),
        "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(raw.get("cache_read_input_tokens") or 0),
    }


async def record_usage(
    *,
    tenant_id: str,
    space_id: str,
    operation: Operation,
    model: str,
    usage: dict | None,
    job_id: str | None = None,
) -> UsageEvent | None:
    """Persist one UsageEvent. Returns the event on success, ``None`` on failure."""
    try:
        normalized = _normalize_usage(usage)
        cost = compute_cost_usd(model=model, **normalized)
        event = UsageEvent(
            id=f"evt_{uuid4().hex[:14]}",
            tenant_id=tenant_id,
            space_id=space_id,
            job_id=job_id,
            operation=operation,
            model=model,
            cost_usd=cost,
            created_at=datetime.now(UTC),
            **normalized,
        )
        db = get_db()
        await db.usage_events.insert_one(event.model_dump())
        return event
    except Exception:
        logger.exception(
            "Failed to record usage event (tenant=%s, space=%s, operation=%s, model=%s)",
            tenant_id,
            space_id,
            operation,
            model,
        )
        return None
