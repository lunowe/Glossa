"""Backfill Tenant documents for existing tenant_id strings on Space records.

For each distinct tenant_id on a Space that doesn't yet have a Tenant row,
create one with placeholder name/owner_email. Safe to re-run.

Usage:
    python -m scripts.backfill_tenants
"""

import asyncio
import logging
from datetime import UTC, datetime

from glossa.config import get_settings
from glossa.db.client import close_db, get_db, init_db
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus

logger = logging.getLogger(__name__)


async def backfill_with_db(db) -> int:
    tenant_ids = await db.spaces.distinct("tenant_id")
    created = 0
    for tid in tenant_ids:
        if not tid:
            continue
        if await db.tenants.find_one({"id": tid}, {"id": 1}):
            continue
        now = datetime.now(UTC)
        tenant = Tenant(
            id=tid,
            name=f"legacy:{tid}",
            owner_email=f"legacy+{tid}@glossa.local",
            plan=TenantPlan.FREE,
            status=TenantStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        await db.tenants.insert_one(tenant.model_dump())
        created += 1
        logger.info("created Tenant for %s", tid)
    return created


async def backfill() -> int:
    settings = get_settings()
    await init_db(settings)
    try:
        return await backfill_with_db(get_db())
    finally:
        await close_db()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    created = asyncio.run(backfill())
    print(f"created {created} Tenant rows")  # noqa: T201
