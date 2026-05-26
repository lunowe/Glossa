"""Tests for Tenant + ApiKey models and the tenant backfill script."""

from datetime import UTC, datetime

from glossa.models.api_key import (
    DEFAULT_SCOPES,
    ApiKey,
    Scope,
    generate_key,
    hash_key,
)
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from scripts.backfill_tenants import backfill_with_db


def test_generate_key_format():
    plaintext, prefix, hashed = generate_key()
    assert plaintext.startswith("glsk_live_")
    assert prefix.startswith("glsk_live_")
    # prefix is "glsk_live_" + 8 chars of random part
    assert len(prefix) - len("glsk_live_") == 8
    assert hash_key(plaintext) == hashed


def test_hash_key_deterministic():
    plaintext = "glsk_live_some_secret_value"
    assert hash_key(plaintext) == hash_key(plaintext)


def test_tenant_pydantic_roundtrip():
    now = datetime.now(UTC)
    tenant = Tenant(
        id="tnt_abc123def456",
        name="Acme",
        owner_email="owner@example.com",
        plan=TenantPlan.PRO,
        status=TenantStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    dumped = tenant.model_dump()
    restored = Tenant.model_validate(dumped)
    assert restored.id == tenant.id
    assert restored.name == tenant.name
    assert restored.owner_email == tenant.owner_email
    assert restored.plan == TenantPlan.PRO
    assert restored.status == TenantStatus.ACTIVE
    assert restored.created_at == tenant.created_at
    assert restored.updated_at == tenant.updated_at


def test_api_key_pydantic_roundtrip():
    now = datetime.now(UTC)
    key = ApiKey(
        id="key_abc123def456",
        tenant_id="tnt_xxx",
        hashed_key="deadbeef",
        prefix="glsk_live_abcd1234",
        created_at=now,
    )
    dumped = key.model_dump()
    restored = ApiKey.model_validate(dumped)
    assert restored.id == key.id
    assert restored.tenant_id == key.tenant_id
    assert restored.hashed_key == key.hashed_key
    assert restored.prefix == key.prefix
    assert restored.label is None
    assert restored.last_used_at is None
    assert restored.revoked_at is None
    assert restored.scopes == list(DEFAULT_SCOPES)


def test_default_scopes_excludes_admin():
    assert Scope.ADMIN not in DEFAULT_SCOPES


async def test_backfill_creates_tenants(mongomock_db):
    now = datetime.now(UTC)
    await mongomock_db.spaces.insert_many(
        [
            {
                "id": "gls_a",
                "tenant_id": "t1",
                "name": "A",
                "slug": "a",
                "bucket_uri": "mem://a/",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "gls_b",
                "tenant_id": "t2",
                "name": "B",
                "slug": "b",
                "bucket_uri": "mem://b/",
                "created_at": now,
                "updated_at": now,
            },
        ]
    )
    created = await backfill_with_db(mongomock_db)
    assert created == 2
    assert await mongomock_db.tenants.count_documents({}) == 2
    assert await mongomock_db.tenants.find_one({"id": "t1"}) is not None
    assert await mongomock_db.tenants.find_one({"id": "t2"}) is not None


async def test_backfill_skips_empty_tenant_id(mongomock_db):
    now = datetime.now(UTC)
    await mongomock_db.spaces.insert_one(
        {
            "id": "gls_empty",
            "tenant_id": "",
            "name": "Empty",
            "slug": "empty",
            "bucket_uri": "mem://empty/",
            "created_at": now,
            "updated_at": now,
        }
    )
    created = await backfill_with_db(mongomock_db)
    assert created == 0
    assert await mongomock_db.tenants.count_documents({}) == 0


async def test_backfill_idempotent(mongomock_db):
    now = datetime.now(UTC)
    await mongomock_db.spaces.insert_one(
        {
            "id": "gls_a",
            "tenant_id": "t1",
            "name": "A",
            "slug": "a",
            "bucket_uri": "mem://a/",
            "created_at": now,
            "updated_at": now,
        }
    )
    first = await backfill_with_db(mongomock_db)
    second = await backfill_with_db(mongomock_db)
    assert first == 1
    assert second == 0
    assert await mongomock_db.tenants.count_documents({}) == 1
