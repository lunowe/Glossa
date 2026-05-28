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

    await db.tenants.create_index("id", unique=True)
    await db.tenants.create_index("owner_email", unique=True)
    await db.api_keys.create_index("id", unique=True)
    await db.api_keys.create_index("hashed_key", unique=True)
    await db.api_keys.create_index("tenant_id")

    await db.request_events.create_index(
        "created_at",
        expireAfterSeconds=7776000,
    )
    await db.request_events.create_index([("tenant_id", 1), ("created_at", -1)])

    await db.users.create_index("id", unique=True)
    await db.users.create_index("email", unique=True)
    await db.users.create_index("oauth_accounts.provider_user_id")

    await db.tenant_members.create_index("id", unique=True)
    await db.tenant_members.create_index([("tenant_id", 1), ("user_id", 1)], unique=True)
    await db.tenant_members.create_index("user_id")  # list-my-tenants query

    await db.sessions.create_index("id", unique=True)
    await db.sessions.create_index("user_id")
    # TTL: sessions auto-prune at expires_at
    await db.sessions.create_index("expires_at", expireAfterSeconds=0)

    await db.oauth_states.create_index("id", unique=True)
    # TTL: states auto-prune at expires_at
    await db.oauth_states.create_index("expires_at", expireAfterSeconds=0)

    await db.invites.create_index("id", unique=True)
    await db.invites.create_index("token", unique=True)
    await db.invites.create_index("tenant_id")
