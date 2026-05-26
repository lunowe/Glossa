from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Operation(StrEnum):
    """Granular label for what a single LLM call was doing.

    One ingest fires roughly 1 extract + N update_page calls + 0 LLM index/log
    writes. One query fires 1 route + 1 answer call. Lint is reserved.
    """

    INGEST_EXTRACT = "ingest.extract"
    INGEST_UPDATE_PAGE = "ingest.update_page"
    QUERY_ROUTE = "query.route"
    QUERY_ANSWER = "query.answer"
    LINT = "lint"


class UsageEvent(BaseModel):
    """One row per LLM call. The raw source of truth for billing and analytics."""

    id: str
    tenant_id: str
    space_id: str
    job_id: str | None = None
    operation: Operation
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime


class UsagePeriodSummary(BaseModel):
    """Rolled-up usage for one tenant over one period (typically a calendar month)."""

    tenant_id: str
    period: str  # YYYY-MM for monthly rollups
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    event_count: int = 0
    by_operation: dict[str, dict] = Field(default_factory=dict)
    by_model: dict[str, dict] = Field(default_factory=dict)


class TenantQuota(BaseModel):
    """Plan-derived limits applied to a single tenant.

    For v0.1 there is no Plan object; each tenant gets a direct override
    document at ``glossa.tenant_quotas``. ``monthly_cost_limit_usd=None``
    means "no limit"; ``=0`` means "blocked".
    """

    tenant_id: str
    monthly_cost_limit_usd: float | None = None
    monthly_token_limit: int | None = None
    allowed_models: list[str] | None = None
    notes: str | None = None
    updated_at: datetime


class TenantQuotaUpdate(BaseModel):
    monthly_cost_limit_usd: float | None = None
    monthly_token_limit: int | None = None
    allowed_models: list[str] | None = None
    notes: str | None = None


class QuotaStatus(BaseModel):
    """What the host (Chatforen) needs to render a quota gauge or block UI."""

    tenant_id: str
    period: str
    cost_usd_used: float
    cost_usd_limit: float | None
    cost_usd_remaining: float | None
    token_used: int
    token_limit: int | None
    token_remaining: int | None
    blocked: bool
