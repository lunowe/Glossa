"""Tests for the activity (request_events) stack: middleware, recorder, router."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from glossa.activity.middleware import ActivityMiddleware
from glossa.activity.models import RequestEvent
from glossa.auth import get_auth_context
from glossa.config import Settings
from glossa.db.client import get_db
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.routes import activity as activity_routes
from glossa.storage.memory import InMemoryStorageBackend

ALICE_TOKEN = "glsk_live_alice_activity_secret"
BOB_TOKEN = "glsk_live_bob_activity_secret"
ADMIN_TOKEN = "glsk_live_admin_activity_secret"

ALICE_HEADERS = {"Authorization": f"Bearer {ALICE_TOKEN}"}
BOB_HEADERS = {"Authorization": f"Bearer {BOB_TOKEN}"}
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(auth_required: bool = True) -> Settings:
    return Settings(auth_required=auth_required)


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


async def _seed_api_key(
    db,
    *,
    plaintext: str,
    tenant_id: str,
    key_id: str,
    scopes: list[Scope] | None = None,
) -> ApiKey:
    now = datetime.now(UTC)
    api_key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        hashed_key=hash_key(plaintext),
        prefix=plaintext[: len("glsk_live_") + 8],
        scopes=scopes if scopes is not None else [Scope.SPACES_READ, Scope.SPACES_WRITE, Scope.QUERY, Scope.LINT],
        created_at=now,
    )
    await db.api_keys.insert_one(api_key.model_dump())
    return api_key


async def _seed_request_event(
    db,
    *,
    event_id: str,
    tenant_id: str | None,
    method: str = "GET",
    path: str = "/spaces",
    status_code: int = 200,
    duration_ms: int = 5,
    error: str | None = None,
    created_at: datetime | None = None,
) -> RequestEvent:
    event = RequestEvent(
        id=event_id,
        tenant_id=tenant_id,
        api_key_id=None,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        created_at=created_at or datetime.now(UTC),
        error=error,
    )
    await db.request_events.insert_one(event.model_dump())
    return event


# --- Middleware tests -----------------------------------------------------------


def _make_middleware_app(*, auth_required: bool = False) -> FastAPI:
    """Minimal FastAPI app for exercising the middleware in isolation."""
    test_app = FastAPI()
    test_app.state.settings = _settings(auth_required=auth_required)
    test_app.state.storage = InMemoryStorageBackend()
    test_app.add_middleware(ActivityMiddleware)

    @test_app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @test_app.get("/echo", dependencies=[Depends(get_auth_context)])
    async def echo() -> dict:
        return {"ok": True}

    @test_app.get("/boom", dependencies=[Depends(get_auth_context)])
    async def boom() -> dict:
        raise RuntimeError("kaboom")

    return test_app


async def test_middleware_records_request_for_healthz_skipped(mongomock_db):
    test_app = _make_middleware_app(auth_required=False)
    client = TestClient(test_app, raise_server_exceptions=False)

    resp = client.get("/healthz")
    assert resp.status_code == 200

    db = get_db()
    count = await db.request_events.count_documents({})
    assert count == 0


async def test_middleware_records_request_for_other_paths(mongomock_db):
    test_app = _make_middleware_app(auth_required=False)
    client = TestClient(test_app, raise_server_exceptions=False)

    resp = client.get("/echo")
    assert resp.status_code == 200

    db = get_db()
    docs = [doc async for doc in db.request_events.find({})]
    assert len(docs) == 1
    assert docs[0]["method"] == "GET"
    assert docs[0]["path"] == "/echo"
    assert docs[0]["status_code"] == 200


async def test_middleware_records_tenant_id_when_auth_resolves(mongomock_db):
    test_app = _make_middleware_app(auth_required=True)
    client = TestClient(test_app, raise_server_exceptions=False)

    await _seed_tenant(mongomock_db, "tnt_alice")
    await _seed_api_key(
        mongomock_db,
        plaintext=ALICE_TOKEN,
        tenant_id="tnt_alice",
        key_id="key_alice",
    )

    resp = client.get("/echo", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text

    db = get_db()
    docs = [doc async for doc in db.request_events.find({})]
    assert len(docs) == 1
    assert docs[0]["tenant_id"] == "tnt_alice"
    assert docs[0]["api_key_id"] == "key_alice"


async def test_middleware_records_status_500_as_error_category(mongomock_db):
    test_app = _make_middleware_app(auth_required=False)
    client = TestClient(test_app, raise_server_exceptions=False)

    resp = client.get("/boom")
    assert resp.status_code == 500

    db = get_db()
    docs = [doc async for doc in db.request_events.find({})]
    assert len(docs) == 1
    assert docs[0]["status_code"] == 500
    assert docs[0]["error"] == "server_error"


async def test_middleware_records_duration_ms(mongomock_db):
    test_app = _make_middleware_app(auth_required=False)
    client = TestClient(test_app, raise_server_exceptions=False)

    resp = client.get("/echo")
    assert resp.status_code == 200

    db = get_db()
    docs = [doc async for doc in db.request_events.find({})]
    assert len(docs) == 1
    assert docs[0]["duration_ms"] >= 0


async def test_recorder_failure_does_not_crash_request(mongomock_db, monkeypatch):
    test_app = _make_middleware_app(auth_required=False)
    client = TestClient(test_app, raise_server_exceptions=False)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(mongomock_db.request_events, "insert_one", _boom)

    resp = client.get("/echo")
    assert resp.status_code == 200


async def test_middleware_records_unauthenticated_request_with_null_tenant(mongomock_db):
    """A 401 from the auth dep should still produce a row with tenant_id=None."""
    test_app = _make_middleware_app(auth_required=True)
    client = TestClient(test_app, raise_server_exceptions=False)

    resp = client.get("/echo")  # no Authorization header
    assert resp.status_code == 401

    db = get_db()
    docs = [doc async for doc in db.request_events.find({})]
    assert len(docs) == 1
    assert docs[0]["tenant_id"] is None
    assert docs[0]["api_key_id"] is None
    assert docs[0]["status_code"] == 401


# --- Router tests ---------------------------------------------------------------


def _make_router_app() -> FastAPI:
    """A bare FastAPI app with just the activity router (no middleware).

    We deliberately exclude ``ActivityMiddleware`` here so the router tests
    only see the events seeded by the test itself — middleware behavior is
    covered separately by the middleware tests above.
    """
    test_app = FastAPI()
    test_app.state.settings = _settings(auth_required=True)
    test_app.state.storage = InMemoryStorageBackend()
    test_app.include_router(activity_routes.router)
    return test_app


@pytest.fixture
def router_client():
    return TestClient(_make_router_app())


@pytest.fixture
async def two_tenant_world(mongomock_db):
    await _seed_tenant(mongomock_db, "tnt_alice")
    await _seed_tenant(mongomock_db, "tnt_bob")
    await _seed_api_key(
        mongomock_db,
        plaintext=ALICE_TOKEN,
        tenant_id="tnt_alice",
        key_id="key_alice",
    )
    await _seed_api_key(
        mongomock_db,
        plaintext=BOB_TOKEN,
        tenant_id="tnt_bob",
        key_id="key_bob",
    )
    return {"alice": "tnt_alice", "bob": "tnt_bob"}


@pytest.fixture
async def admin_key(mongomock_db):
    await _seed_api_key(
        mongomock_db,
        plaintext=ADMIN_TOKEN,
        tenant_id="tnt_alice",
        key_id="key_admin_act",
        scopes=[Scope.ADMIN],
    )


async def test_list_requests_returns_own_tenant(router_client, mongomock_db, two_tenant_world):
    await _seed_request_event(mongomock_db, event_id="req_a1", tenant_id="tnt_alice", path="/spaces")
    await _seed_request_event(mongomock_db, event_id="req_b1", tenant_id="tnt_bob", path="/spaces")

    resp = router_client.get("/tenants/tnt_alice/activity/requests", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {row["id"] for row in body}
    assert ids == {"req_a1"}
    assert body[0]["tenant_id"] == "tnt_alice"


async def test_list_requests_other_tenant_returns_404(router_client, mongomock_db, two_tenant_world):
    resp = router_client.get("/tenants/tnt_bob/activity/requests", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


async def test_list_requests_admin_can_query_any(router_client, mongomock_db, two_tenant_world, admin_key):
    await _seed_request_event(mongomock_db, event_id="req_b2", tenant_id="tnt_bob", path="/spaces")
    resp = router_client.get("/tenants/tnt_bob/activity/requests", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {"req_b2"}


async def test_list_requests_method_filter(router_client, mongomock_db, two_tenant_world):
    await _seed_request_event(mongomock_db, event_id="req_get", tenant_id="tnt_alice", method="GET", path="/spaces")
    await _seed_request_event(mongomock_db, event_id="req_post", tenant_id="tnt_alice", method="POST", path="/spaces")
    resp = router_client.get(
        "/tenants/tnt_alice/activity/requests",
        headers=ALICE_HEADERS,
        params={"method": "post"},
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {"req_post"}


async def test_list_requests_path_prefix_filter(router_client, mongomock_db, two_tenant_world):
    await _seed_request_event(mongomock_db, event_id="req_sp", tenant_id="tnt_alice", path="/spaces/gls_a")
    await _seed_request_event(mongomock_db, event_id="req_tn", tenant_id="tnt_alice", path="/tenants/tnt_alice/usage")
    resp = router_client.get(
        "/tenants/tnt_alice/activity/requests",
        headers=ALICE_HEADERS,
        params={"path_prefix": "/spaces"},
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {"req_sp"}


async def test_list_requests_status_min_filter(router_client, mongomock_db, two_tenant_world):
    await _seed_request_event(
        mongomock_db,
        event_id="req_200",
        tenant_id="tnt_alice",
        status_code=200,
    )
    await _seed_request_event(
        mongomock_db,
        event_id="req_500",
        tenant_id="tnt_alice",
        status_code=500,
        error="server_error",
    )
    resp = router_client.get(
        "/tenants/tnt_alice/activity/requests",
        headers=ALICE_HEADERS,
        params={"status_min": 500},
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {"req_500"}


async def test_summary_groups_by_status_and_path(router_client, mongomock_db, two_tenant_world):
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=1)
    await _seed_request_event(
        mongomock_db,
        event_id="req_s1",
        tenant_id="tnt_alice",
        path="/spaces",
        status_code=200,
        duration_ms=10,
        created_at=recent,
    )
    await _seed_request_event(
        mongomock_db,
        event_id="req_s2",
        tenant_id="tnt_alice",
        path="/spaces",
        status_code=200,
        duration_ms=20,
        created_at=recent,
    )
    await _seed_request_event(
        mongomock_db,
        event_id="req_s3",
        tenant_id="tnt_alice",
        path="/pages",
        status_code=500,
        duration_ms=30,
        error="server_error",
        created_at=recent,
    )

    resp = router_client.get("/tenants/tnt_alice/activity/summary", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["tenant_id"] == "tnt_alice"
    assert body["request_count"] == 3
    assert body["error_count"] == 1
    assert body["by_status"]["200"] == 2
    assert body["by_status"]["500"] == 1
    assert body["by_path"]["/spaces"] == 2
    assert body["by_path"]["/pages"] == 1
    # avg = (10 + 20 + 30) / 3 = 20
    assert body["avg_duration_ms"] == 20.0


async def test_summary_avg_duration_zero_when_no_requests(router_client, mongomock_db, two_tenant_world):
    resp = router_client.get("/tenants/tnt_alice/activity/summary", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_count"] == 0
    assert body["error_count"] == 0
    assert body["avg_duration_ms"] == 0.0
    assert body["by_status"] == {}
    assert body["by_path"] == {}
