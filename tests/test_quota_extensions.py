"""Phase 6 quota extensions: source-count, storage-bytes, rate-limit.

Three new dimensions added to ``TenantQuota``. All three are optional and
unset = unlimited. Existing tenants without a quota row stay unlimited on
all dimensions. System contexts are exempt from per-tenant quotas.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.db.client import get_db
from glossa.ingest import page_writer
from glossa.main import app
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.page import PageKind
from glossa.models.space import Space, SpaceStats
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.storage.memory import InMemoryStorageBackend
from glossa.usage import rate_limit
from glossa.usage.quota import (
    QuotaExceededError,
    check_quota,
    check_source_quota,
    check_storage_quota_before_write,
    get_quota_status,
    upsert_quota,
)

ALICE_TOKEN = "glsk_live_alice_quota_value"
BOB_TOKEN = "glsk_live_bob_quota_value"

ALICE_HEADERS = {"Authorization": f"Bearer {ALICE_TOKEN}"}
BOB_HEADERS = {"Authorization": f"Bearer {BOB_TOKEN}"}


def _settings(auth_required: bool = True) -> Settings:
    return Settings(auth_required=auth_required)


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """Each test gets a clean in-memory rate limiter."""
    rate_limit.reset_rate_limiter()
    yield
    rate_limit.reset_rate_limiter()


@pytest.fixture
def storage() -> InMemoryStorageBackend:
    return InMemoryStorageBackend()


@pytest.fixture
def client(storage):
    app.state.settings = _settings(auth_required=True)
    app.state.storage = storage
    return TestClient(app)


async def _seed_tenant(db, tenant_id: str) -> Tenant:
    now = datetime.now(UTC)
    tenant = Tenant(
        id=tenant_id,
        name=tenant_id,
        owner_email=f"{tenant_id}@example.com",
        plan=TenantPlan.FREE,
        status=TenantStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    await db.tenants.insert_one(tenant.model_dump())
    return tenant


async def _seed_api_key(db, *, plaintext: str, tenant_id: str, key_id: str) -> ApiKey:
    now = datetime.now(UTC)
    api_key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        hashed_key=hash_key(plaintext),
        prefix=plaintext[: len("glsk_live_") + 8],
        scopes=[
            Scope.SPACES_READ,
            Scope.SPACES_WRITE,
            Scope.SOURCES_WRITE,
            Scope.QUERY,
            Scope.LINT,
        ],
        created_at=now,
    )
    await db.api_keys.insert_one(api_key.model_dump())
    return api_key


async def _seed_space(db, storage, *, space_id: str, tenant_id: str, slug: str) -> Space:
    now = datetime.now(UTC)
    space = Space(
        id=space_id,
        tenant_id=tenant_id,
        name=f"{tenant_id}-space",
        slug=slug,
        bucket_uri=f"mem://{space_id}/",
        stats=SpaceStats(),
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space(space_id)
    return space


@pytest.fixture
async def two_tenant_world(mongomock_db, storage):
    await _seed_tenant(mongomock_db, "tnt_alice")
    await _seed_tenant(mongomock_db, "tnt_bob")
    await _seed_api_key(mongomock_db, plaintext=ALICE_TOKEN, tenant_id="tnt_alice", key_id="key_alice")
    await _seed_api_key(mongomock_db, plaintext=BOB_TOKEN, tenant_id="tnt_bob", key_id="key_bob")
    alice_space = await _seed_space(mongomock_db, storage, space_id="gls_alice", tenant_id="tnt_alice", slug="alice")
    bob_space = await _seed_space(mongomock_db, storage, space_id="gls_bob", tenant_id="tnt_bob", slug="bob")
    return {"alice_space": alice_space, "bob_space": bob_space}


# --- Model persistence ---------------------------------------------------------


class TestUpsertQuota:
    async def test_upsert_quota_persists_new_fields(self):
        quota = await upsert_quota(
            tenant_id="t1",
            monthly_cost_limit_usd=10.0,
            max_sources_per_space=5,
            max_storage_bytes=1024 * 1024,
            max_requests_per_minute=60,
        )
        assert quota.max_sources_per_space == 5
        assert quota.max_storage_bytes == 1024 * 1024
        assert quota.max_requests_per_minute == 60

        # Round-trip through DB
        db = get_db()
        doc = await db.tenant_quotas.find_one({"tenant_id": "t1"})
        assert doc is not None
        assert doc["max_sources_per_space"] == 5
        assert doc["max_storage_bytes"] == 1024 * 1024
        assert doc["max_requests_per_minute"] == 60

    async def test_unset_new_fields_means_unlimited(self):
        await upsert_quota(tenant_id="t1", monthly_cost_limit_usd=10.0)
        status = await get_quota_status("t1")
        assert status.sources_per_space_max is None
        assert status.storage_bytes_limit is None
        assert status.requests_per_minute_limit is None


class TestQuotaStatusPopulatesNewFields:
    async def test_quota_status_populates_new_fields(self, mongomock_db, storage):
        await _seed_tenant(mongomock_db, "tnt_alice")
        await _seed_space(mongomock_db, storage, space_id="gls_alice", tenant_id="tnt_alice", slug="alice")
        # Insert page rows directly so we control size_bytes.
        now = datetime.now(UTC)
        await mongomock_db.pages.insert_one(
            {
                "space_id": "gls_alice",
                "path": "entities/a",
                "kind": PageKind.ENTITY.value,
                "title": "A",
                "frontmatter": {},
                "source_refs": [],
                "backlinks": [],
                "size_bytes": 128,
                "updated_at": now,
                "last_touched_by_job_id": None,
            }
        )
        await mongomock_db.pages.insert_one(
            {
                "space_id": "gls_alice",
                "path": "entities/b",
                "kind": PageKind.ENTITY.value,
                "title": "B",
                "frontmatter": {},
                "source_refs": [],
                "backlinks": [],
                "size_bytes": 256,
                "updated_at": now,
                "last_touched_by_job_id": None,
            }
        )
        # Insert source rows
        for sid in ("src_1", "src_2", "src_3"):
            await mongomock_db.sources.insert_one(
                {
                    "id": sid,
                    "space_id": "gls_alice",
                    "title": sid,
                    "ingestion_mode": "push",
                    "content_inline": "x",
                    "fetch_callback": None,
                    "external_uri": None,
                    "metadata": {},
                    "status": "received",
                    "created_at": now,
                    "last_ingested_at": None,
                    "last_ingest_job_id": None,
                }
            )
        await upsert_quota(
            tenant_id="tnt_alice",
            max_sources_per_space=10,
            max_storage_bytes=2000,
            max_requests_per_minute=30,
        )
        status = await get_quota_status("tnt_alice")
        assert status.sources_per_space_max == 10
        assert status.sources_per_space_used_max == 3
        assert status.storage_bytes_used == 384
        assert status.storage_bytes_limit == 2000
        assert status.requests_per_minute_limit == 30
        assert status.requests_per_minute_used == 0


# --- max_sources_per_space ------------------------------------------------------


class TestMaxSourcesPerSpace:
    async def test_max_sources_per_space_blocks_creation(self, client, two_tenant_world):
        await upsert_quota(tenant_id="tnt_alice", max_sources_per_space=2)

        payload = {
            "title": "s",
            "ingestion_mode": "push",
            "content_inline": "hello",
        }

        # First two succeed.
        resp1 = client.post("/spaces/gls_alice/sources", json=payload, headers=ALICE_HEADERS)
        assert resp1.status_code == 200, resp1.text
        resp2 = client.post("/spaces/gls_alice/sources", json=payload, headers=ALICE_HEADERS)
        assert resp2.status_code == 200, resp2.text

        # Third blocked with 402 + structured detail.
        resp3 = client.post("/spaces/gls_alice/sources", json=payload, headers=ALICE_HEADERS)
        assert resp3.status_code == 402, resp3.text
        detail = resp3.json()["detail"]
        assert "Source count limit reached" in detail["reason"]
        assert detail["quota"]["sources_per_space_max"] == 2

    async def test_max_sources_per_space_isolated_per_space(self, client, mongomock_db, storage, two_tenant_world):
        """Per-space limit means a different space of the same tenant is unaffected."""
        await _seed_space(
            mongomock_db,
            storage,
            space_id="gls_alice_2",
            tenant_id="tnt_alice",
            slug="alice-two",
        )
        await upsert_quota(tenant_id="tnt_alice", max_sources_per_space=1)
        payload = {"title": "x", "ingestion_mode": "push", "content_inline": "hi"}

        # space 1: take the slot
        r1 = client.post("/spaces/gls_alice/sources", json=payload, headers=ALICE_HEADERS)
        assert r1.status_code == 200, r1.text
        # space 1: second blocked
        r2 = client.post("/spaces/gls_alice/sources", json=payload, headers=ALICE_HEADERS)
        assert r2.status_code == 402, r2.text
        # space 2: independent, fresh slot
        r3 = client.post("/spaces/gls_alice_2/sources", json=payload, headers=ALICE_HEADERS)
        assert r3.status_code == 200, r3.text

    async def test_max_sources_per_space_system_context_exempt(self, mongomock_db, storage):
        """Self-host system context (auth_required=False) bypasses the per-space cap."""
        await _seed_space(mongomock_db, storage, space_id="gls_sys", tenant_id="_system", slug="sys")
        await upsert_quota(tenant_id="_system", max_sources_per_space=1)

        app.state.settings = _settings(auth_required=False)
        app.state.storage = storage
        local_client = TestClient(app)
        payload = {"title": "x", "ingestion_mode": "push", "content_inline": "hi"}

        # No Authorization header: AuthContext.system() is used, exempt.
        r1 = local_client.post("/spaces/gls_sys/sources", json=payload)
        assert r1.status_code == 200, r1.text
        r2 = local_client.post("/spaces/gls_sys/sources", json=payload)
        assert r2.status_code == 200, r2.text

    async def test_check_source_quota_called_with_real_tenant_still_blocks(self, mongomock_db, storage):
        """Direct call to check_source_quota proves the gate works without HTTP."""
        await _seed_tenant(mongomock_db, "tnt_x")
        await _seed_space(mongomock_db, storage, space_id="gls_x", tenant_id="tnt_x", slug="x")
        await upsert_quota(tenant_id="tnt_x", max_sources_per_space=1)
        now = datetime.now(UTC)
        await mongomock_db.sources.insert_one(
            {
                "id": "src_a",
                "space_id": "gls_x",
                "title": "a",
                "ingestion_mode": "push",
                "content_inline": "x",
                "fetch_callback": None,
                "external_uri": None,
                "metadata": {},
                "status": "received",
                "created_at": now,
                "last_ingested_at": None,
                "last_ingest_job_id": None,
            }
        )
        with pytest.raises(QuotaExceededError) as exc:
            await check_source_quota("tnt_x", "gls_x")
        assert "Source count limit" in exc.value.reason


# --- max_storage_bytes ----------------------------------------------------------


class TestMaxStorageBytes:
    async def test_max_storage_bytes_blocks_write(self, mongomock_db, storage):
        await _seed_tenant(mongomock_db, "tnt_x")
        await _seed_space(mongomock_db, storage, space_id="gls_x", tenant_id="tnt_x", slug="x")
        await upsert_quota(tenant_id="tnt_x", max_storage_bytes=100)

        # Below limit: write 50 bytes -> OK.
        await check_storage_quota_before_write("tnt_x", 50)

        # Now insert a page row reflecting that 50 bytes were persisted.
        now = datetime.now(UTC)
        await mongomock_db.pages.insert_one(
            {
                "space_id": "gls_x",
                "path": "entities/a",
                "kind": PageKind.ENTITY.value,
                "title": "A",
                "frontmatter": {},
                "source_refs": [],
                "backlinks": [],
                "size_bytes": 50,
                "updated_at": now,
                "last_touched_by_job_id": None,
            }
        )

        # 50 + 60 > 100 -> blocked.
        with pytest.raises(QuotaExceededError) as exc:
            await check_storage_quota_before_write("tnt_x", 60)
        assert "Storage bytes limit" in exc.value.reason

        # 50 + 30 = 80 <= 100 -> ok.
        await check_storage_quota_before_write("tnt_x", 30)

    async def test_no_quota_means_unlimited(self, mongomock_db, storage):
        await _seed_tenant(mongomock_db, "tnt_x")
        await _seed_space(mongomock_db, storage, space_id="gls_x", tenant_id="tnt_x", slug="x")
        # No upsert_quota call.
        await check_storage_quota_before_write("tnt_x", 10_000_000)

    async def test_put_schema_respects_storage_quota(self, client, two_tenant_world):
        await upsert_quota(tenant_id="tnt_alice", max_storage_bytes=20)
        resp = client.put(
            "/spaces/gls_alice/schema",
            headers=ALICE_HEADERS,
            params={"schema_markdown": "x" * 50},
        )
        assert resp.status_code == 402, resp.text
        detail = resp.json()["detail"]
        assert "Storage bytes limit" in detail["reason"]


# --- max_requests_per_minute ----------------------------------------------------


class TestRateLimit:
    async def test_rate_limit_blocks_when_exceeded(self):
        await upsert_quota(tenant_id="tnt_x", max_requests_per_minute=2)
        # Two allowed, third blocked.
        await check_quota("tnt_x")
        await check_quota("tnt_x")
        with pytest.raises(QuotaExceededError) as exc:
            await check_quota("tnt_x")
        assert "Rate limit" in exc.value.reason

    async def test_rate_limit_does_not_block_below_limit(self):
        await upsert_quota(tenant_id="tnt_x", max_requests_per_minute=5)
        for _ in range(4):
            await check_quota("tnt_x")
        # Still under cap.
        status = await get_quota_status("tnt_x")
        assert status.blocked is False
        assert status.requests_per_minute_used == 4

    async def test_rate_limit_resets_after_window(self, monkeypatch):
        """After the window elapses, old hits drop and new calls succeed."""
        await upsert_quota(tenant_id="tnt_x", max_requests_per_minute=1)
        await check_quota("tnt_x")
        # Second call inside window: blocked.
        with pytest.raises(QuotaExceededError):
            await check_quota("tnt_x")

        # Fast-forward monotonic time past WINDOW_SECONDS.
        import time as _time

        from glossa.usage import rate_limit as rl

        real_monotonic = _time.monotonic
        future = real_monotonic() + rl.WINDOW_SECONDS + 1.0
        monkeypatch.setattr(rl.time, "monotonic", lambda: future)

        # Old hit is past cutoff; new call accepted.
        await check_quota("tnt_x")

    async def test_rate_limit_per_tenant_isolation(self):
        await upsert_quota(tenant_id="tnt_alice", max_requests_per_minute=1)
        await upsert_quota(tenant_id="tnt_bob", max_requests_per_minute=1)

        await check_quota("tnt_alice")
        with pytest.raises(QuotaExceededError):
            await check_quota("tnt_alice")

        # Bob is unaffected.
        await check_quota("tnt_bob")

    async def test_rate_limit_unset_means_unlimited(self):
        # No quota row at all.
        for _ in range(20):
            await check_quota("tnt_unlimited")

    async def test_rate_limit_concurrent_safety(self):
        """Concurrent ``check_quota`` calls must not let extra hits through."""
        await upsert_quota(tenant_id="tnt_x", max_requests_per_minute=5)

        async def hit() -> int:
            try:
                await check_quota("tnt_x")
                return 1
            except QuotaExceededError:
                return 0

        results = await asyncio.gather(*(hit() for _ in range(20)))
        assert sum(results) == 5


# --- page_writer plumbing -------------------------------------------------------


class TestPageWriterRecordsSizeBytes:
    async def test_page_writer_records_size_bytes(self, storage, mongomock_db):
        await _seed_tenant(mongomock_db, "tnt_x")
        await _seed_space(mongomock_db, storage, space_id="gls_x", tenant_id="tnt_x", slug="x")

        content = "# Hello\n\nThis is a test page with some bytes.\n"
        expected = len(content.encode("utf-8"))
        is_new, is_changed = await page_writer.upsert_page(
            storage=storage,
            space_id="gls_x",
            page_path="entities/test",
            kind=PageKind.ENTITY,
            title="Test",
            new_content=content,
            source_refs=["src_test"],
            job_id="job_t",
        )
        assert is_new and is_changed
        doc = await mongomock_db.pages.find_one({"space_id": "gls_x", "path": "entities/test"})
        assert doc is not None
        assert doc["size_bytes"] == expected

    async def test_page_writer_storage_quota_blocks_when_tenant_id_provided(self, storage, mongomock_db):
        await _seed_tenant(mongomock_db, "tnt_x")
        await _seed_space(mongomock_db, storage, space_id="gls_x", tenant_id="tnt_x", slug="x")
        await upsert_quota(tenant_id="tnt_x", max_storage_bytes=10)

        with pytest.raises(QuotaExceededError):
            await page_writer.upsert_page(
                storage=storage,
                space_id="gls_x",
                page_path="entities/big",
                kind=PageKind.ENTITY,
                title="Big",
                new_content="x" * 100,
                source_refs=[],
                job_id="job_t",
                tenant_id="tnt_x",
            )
