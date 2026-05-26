"""Tests for the usage recording, aggregation, and quota stack."""

from datetime import UTC, datetime, timedelta

import pytest

from glossa.db.client import get_db
from glossa.usage.aggregator import aggregate_period, aggregate_tenant_by_space, period_for
from glossa.usage.models import Operation
from glossa.usage.quota import (
    QuotaExceededError,
    check_quota,
    get_quota_status,
    upsert_quota,
)
from glossa.usage.recorder import _normalize_usage, record_usage


class TestNormalizeUsage:
    def test_anthropic_shape_passthrough(self):
        result = _normalize_usage(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 200,
            }
        )
        assert result == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 200,
        }

    def test_openai_shape_mapping(self):
        result = _normalize_usage({"prompt_tokens": 120, "completion_tokens": 60})
        assert result["input_tokens"] == 120
        assert result["output_tokens"] == 60
        assert result["cache_creation_input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0

    def test_none_returns_zeros(self):
        result = _normalize_usage(None)
        assert all(v == 0 for v in result.values())

    def test_missing_fields_default_to_zero(self):
        result = _normalize_usage({"input_tokens": 42})
        assert result["input_tokens"] == 42
        assert result["output_tokens"] == 0


class TestRecordUsage:
    async def test_records_event_with_computed_cost(self):
        event = await record_usage(
            tenant_id="t1",
            space_id="s1",
            operation=Operation.QUERY_ANSWER,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 500},
        )
        assert event is not None
        assert event.tenant_id == "t1"
        assert event.input_tokens == 1000
        assert event.output_tokens == 500
        # 1k * 3.00 / 1M + 0.5k * 15.00 / 1M = 0.003 + 0.0075 = 0.0105
        assert event.cost_usd == 0.0105

    async def test_persists_to_db(self):
        await record_usage(
            tenant_id="t1",
            space_id="s1",
            operation=Operation.INGEST_EXTRACT,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        db = get_db()
        count = await db.usage_events.count_documents({"tenant_id": "t1"})
        assert count == 1


class TestAggregation:
    async def _seed(self):
        """Insert three events for tenant t1 across two spaces in the current month."""
        await record_usage(
            tenant_id="t1",
            space_id="s_alpha",
            operation=Operation.INGEST_EXTRACT,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1_000_000, "output_tokens": 100_000},
        )
        await record_usage(
            tenant_id="t1",
            space_id="s_alpha",
            operation=Operation.QUERY_ANSWER,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 500_000, "output_tokens": 50_000},
        )
        await record_usage(
            tenant_id="t1",
            space_id="s_beta",
            operation=Operation.INGEST_EXTRACT,
            model="claude-haiku-4-5",
            usage={"input_tokens": 2_000_000, "output_tokens": 100_000},
        )

    async def test_period_totals(self):
        await self._seed()
        summary = await aggregate_period("t1")
        assert summary.event_count == 3
        assert summary.input_tokens == 3_500_000
        assert summary.output_tokens == 250_000
        # Sonnet: 1.5M * 3 / 1M + 150k * 15 / 1M = 4.5 + 2.25 = 6.75
        # Haiku: 2M * 1 / 1M + 100k * 5 / 1M = 2.0 + 0.5 = 2.5
        # Total: 9.25
        assert summary.cost_usd == 9.25
        assert "claude-sonnet-4-6" in summary.by_model
        assert "claude-haiku-4-5" in summary.by_model
        assert summary.by_model["claude-sonnet-4-6"]["cost_usd"] == 6.75

    async def test_by_space(self):
        await self._seed()
        rows = await aggregate_tenant_by_space("t1")
        rows_by_space = {row["space_id"]: row for row in rows}
        assert rows_by_space["s_alpha"]["event_count"] == 2
        assert rows_by_space["s_beta"]["event_count"] == 1

    async def test_excludes_other_tenants(self):
        await self._seed()
        await record_usage(
            tenant_id="t2",
            space_id="other",
            operation=Operation.INGEST_EXTRACT,
            model="claude-opus-4-7",
            usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        )
        summary = await aggregate_period("t1")
        assert summary.event_count == 3
        assert summary.cost_usd == 9.25


class TestPeriodFor:
    def test_current_month_format(self):
        assert period_for(datetime(2026, 5, 13, tzinfo=UTC)) == "2026-05"

    def test_january(self):
        assert period_for(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01"

    def test_december(self):
        assert period_for(datetime(2026, 12, 31, tzinfo=UTC)) == "2026-12"


class TestQuota:
    async def test_no_quota_means_not_blocked(self):
        status = await get_quota_status("free_tenant")
        assert status.blocked is False
        assert status.cost_usd_limit is None
        assert status.cost_usd_remaining is None

    async def test_under_limit_not_blocked(self):
        await upsert_quota(tenant_id="t1", monthly_cost_limit_usd=100.0)
        await record_usage(
            tenant_id="t1",
            space_id="s1",
            operation=Operation.INGEST_EXTRACT,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 500},
        )
        status = await get_quota_status("t1")
        assert status.blocked is False
        assert status.cost_usd_limit == 100.0
        assert status.cost_usd_used == 0.0105

    async def test_over_limit_blocked(self):
        # Set a tiny limit, then record usage that exceeds it.
        await upsert_quota(tenant_id="t1", monthly_cost_limit_usd=0.001)
        await record_usage(
            tenant_id="t1",
            space_id="s1",
            operation=Operation.INGEST_EXTRACT,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 500},
        )
        status = await get_quota_status("t1")
        assert status.blocked is True
        assert status.cost_usd_remaining == 0.0

    async def test_check_quota_raises_when_over(self):
        await upsert_quota(tenant_id="t1", monthly_cost_limit_usd=0.001)
        await record_usage(
            tenant_id="t1",
            space_id="s1",
            operation=Operation.INGEST_EXTRACT,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 500},
        )
        with pytest.raises(QuotaExceededError) as exc_info:
            await check_quota("t1")
        assert "cost limit" in exc_info.value.reason.lower()
        assert exc_info.value.status.blocked is True

    async def test_token_limit_blocks_independently_of_cost(self):
        await upsert_quota(tenant_id="t1", monthly_token_limit=100)
        await record_usage(
            tenant_id="t1",
            space_id="s1",
            operation=Operation.INGEST_EXTRACT,
            model="claude-sonnet-4-6",
            usage={"input_tokens": 200, "output_tokens": 0},
        )
        with pytest.raises(QuotaExceededError) as exc_info:
            await check_quota("t1")
        assert "token limit" in exc_info.value.reason.lower()

    async def test_previous_month_events_dont_count(self):
        await upsert_quota(tenant_id="t1", monthly_cost_limit_usd=0.001)
        # Manually insert a past-month event
        db = get_db()
        old_event = {
            "id": "evt_old",
            "tenant_id": "t1",
            "space_id": "s1",
            "operation": "ingest.extract",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cost_usd": 18.0,
            "job_id": None,
            "created_at": datetime.now(UTC) - timedelta(days=45),
        }
        await db.usage_events.insert_one(old_event)
        status = await get_quota_status("t1")
        assert status.blocked is False
        assert status.cost_usd_used == 0.0
