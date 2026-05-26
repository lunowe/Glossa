"""Record one RequestEvent per HTTP request.

Fire-and-forget — recording failures are logged but never crash the request.
Pattern matches glossa/usage/recorder.py.
"""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from glossa.activity.models import RequestEvent
from glossa.db.client import get_db

logger = logging.getLogger(__name__)


async def record_request(
    *,
    tenant_id: str | None,
    api_key_id: str | None,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    error: str | None = None,
) -> RequestEvent | None:
    try:
        event = RequestEvent(
            id=f"req_{uuid4().hex[:14]}",
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            created_at=datetime.now(UTC),
            error=error,
        )
        db = get_db()
        await db.request_events.insert_one(event.model_dump())
        return event
    except Exception:
        logger.exception(
            "Failed to record request_event (tenant=%s, method=%s, path=%s, status=%s)",
            tenant_id,
            method,
            path,
            status_code,
        )
        return None
