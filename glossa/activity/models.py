from datetime import datetime

from pydantic import BaseModel


class RequestEvent(BaseModel):
    """One row per HTTP request. The raw source of truth for activity audit."""

    id: str
    tenant_id: str | None
    api_key_id: str | None
    method: str
    path: str
    status_code: int
    duration_ms: int
    created_at: datetime
    error: str | None = None  # short error category if status >= 500


class RequestActivitySummary(BaseModel):
    """Aggregate rollup for a tenant + period."""

    tenant_id: str
    period_start: datetime
    period_end: datetime
    request_count: int = 0
    error_count: int = 0
    avg_duration_ms: float = 0.0
    by_status: dict[str, int] = {}
    by_path: dict[str, int] = {}
