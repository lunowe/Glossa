from typing import TYPE_CHECKING

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

if TYPE_CHECKING:
    from glossa.config import Settings


_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def init_db(settings: "Settings") -> None:
    global _client, _db
    _client = AsyncIOMotorClient(settings.mongo_uri)
    _db = _client[settings.mongo_db]
    await _create_indexes(_db)


async def close_db() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("DB not initialized — call init_db() first.")
    return _db


async def _create_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.spaces.create_index("id", unique=True)
    await db.spaces.create_index([("tenant_id", 1), ("slug", 1)], unique=True)

    await db.sources.create_index("id", unique=True)
    await db.sources.create_index([("space_id", 1), ("created_at", -1)])

    await db.pages.create_index("space_id")
    await db.pages.create_index([("space_id", 1), ("path", 1)], unique=True)

    await db.jobs.create_index("id", unique=True)
    await db.jobs.create_index([("space_id", 1), ("created_at", -1)])

    await db.webhooks.create_index("id", unique=True)
    await db.webhooks.create_index("space_id")

    await db.usage_events.create_index("id", unique=True)
    await db.usage_events.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.usage_events.create_index([("space_id", 1), ("created_at", -1)])
    await db.usage_events.create_index([("tenant_id", 1), ("operation", 1)])

    await db.tenant_quotas.create_index("tenant_id", unique=True)
