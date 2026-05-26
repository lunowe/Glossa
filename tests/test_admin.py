"""Tests for admin tenant-management routes (/admin/tenants ...).

The admin router can't go through ``glossa.main.app`` here because that
module isn't wired with the new router yet (the orchestrator will wire it
later). We build our own FastAPI app in a fixture and include just the
admin router; auth and the mongomock DB fixture are inherited from
``conftest.py``.
"""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from glossa.auth import get_auth_context
from glossa.config import Settings
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.routes.admin import router as admin_router

ADMIN_TOKEN = "glsk_live_admin_secret"
USER_TOKEN = "glsk_live_user_secret"

ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
USER_HEADERS = {"Authorization": f"Bearer {USER_TOKEN}"}


def _settings() -> Settings:
    return Settings(auth_required=True)


@pytest.fixture
def app():
    """Build a fresh FastAPI app wired with just the admin router."""
    app = FastAPI()
    app.state.settings = _settings()
    app.include_router(admin_router, dependencies=[Depends(get_auth_context)])
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


async def _seed_tenant(
    db,
    *,
    tenant_id: str,
    owner_email: str | None = None,
    status: TenantStatus = TenantStatus.ACTIVE,
    plan: TenantPlan = TenantPlan.FREE,
) -> Tenant:
    now = datetime.now(UTC)
    tenant = Tenant(
        id=tenant_id,
        name=tenant_id,
        owner_email=owner_email or f"{tenant_id}@example.com",
        plan=plan,
        status=status,
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
        scopes=scopes
        if scopes is not None
        else [Scope.SPACES_READ, Scope.SPACES_WRITE, Scope.SOURCES_WRITE, Scope.QUERY, Scope.LINT],
        created_at=now,
    )
    await db.api_keys.insert_one(api_key.model_dump())
    return api_key


@pytest.fixture
async def admin_world(mongomock_db):
    """Seed an admin key under a host tenant + a plain user key under another tenant.

    Production indexes are created in ``init_db()`` which the test conftest
    doesn't run; we install the ``owner_email`` unique index here so the
    duplicate-key handling in the route can be exercised end-to-end.
    """
    await mongomock_db.tenants.create_index("owner_email", unique=True)
    await _seed_tenant(mongomock_db, tenant_id="tnt_host")
    await _seed_tenant(mongomock_db, tenant_id="tnt_user")
    await _seed_api_key(
        mongomock_db,
        plaintext=ADMIN_TOKEN,
        tenant_id="tnt_host",
        key_id="key_admin",
        scopes=[Scope.ADMIN],
    )
    await _seed_api_key(
        mongomock_db,
        plaintext=USER_TOKEN,
        tenant_id="tnt_user",
        key_id="key_user",
    )


# --- POST /admin/tenants --------------------------------------------------------


async def test_create_tenant_returns_tenant_with_id(client, admin_world):
    resp = client.post(
        "/admin/tenants",
        headers=ADMIN_HEADERS,
        json={"name": "Acme", "owner_email": "owner@acme.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"].startswith("tnt_")
    assert len(body["id"]) == 4 + 12  # "tnt_" + 12 hex
    assert body["name"] == "Acme"
    assert body["owner_email"] == "owner@acme.com"
    assert body["plan"] == TenantPlan.FREE.value
    assert body["status"] == TenantStatus.ACTIVE.value


async def test_create_tenant_requires_admin_scope(client, admin_world):
    resp = client.post(
        "/admin/tenants",
        headers=USER_HEADERS,
        json={"name": "Acme", "owner_email": "owner@acme.com"},
    )
    assert resp.status_code == 403, resp.text


async def test_create_tenant_duplicate_email_returns_409(client, admin_world):
    payload = {"name": "First", "owner_email": "dup@example.com"}
    first = client.post("/admin/tenants", headers=ADMIN_HEADERS, json=payload)
    assert first.status_code == 200, first.text
    second = client.post(
        "/admin/tenants",
        headers=ADMIN_HEADERS,
        json={"name": "Second", "owner_email": "dup@example.com"},
    )
    assert second.status_code == 409, second.text
    assert "owner_email" in second.json()["detail"]


# --- GET /admin/tenants ---------------------------------------------------------


async def test_list_tenants_filters_by_status(client, admin_world, mongomock_db):
    await _seed_tenant(
        mongomock_db,
        tenant_id="tnt_suspended",
        owner_email="susp@example.com",
        status=TenantStatus.SUSPENDED,
    )
    resp = client.get(
        "/admin/tenants",
        headers=ADMIN_HEADERS,
        params={"status": TenantStatus.SUSPENDED.value},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    ids = [r["id"] for r in rows]
    assert "tnt_suspended" in ids
    assert all(r["status"] == TenantStatus.SUSPENDED.value for r in rows)


# --- GET /admin/tenants/{id} ----------------------------------------------------


async def test_get_tenant_404_when_missing(client, admin_world):
    resp = client.get("/admin/tenants/tnt_doesnotexist", headers=ADMIN_HEADERS)
    assert resp.status_code == 404, resp.text


# --- PATCH /admin/tenants/{id} --------------------------------------------------


async def test_patch_tenant_suspend(client, admin_world):
    resp = client.patch(
        "/admin/tenants/tnt_user",
        headers=ADMIN_HEADERS,
        json={"status": TenantStatus.SUSPENDED.value},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == TenantStatus.SUSPENDED.value

    # And we can reactivate
    resp = client.patch(
        "/admin/tenants/tnt_user",
        headers=ADMIN_HEADERS,
        json={"status": TenantStatus.ACTIVE.value},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == TenantStatus.ACTIVE.value


async def test_patch_tenant_non_admin_forbidden(client, admin_world):
    resp = client.patch(
        "/admin/tenants/tnt_user",
        headers=USER_HEADERS,
        json={"name": "Renamed"},
    )
    assert resp.status_code == 403, resp.text


async def test_patch_tenant_change_plan(client, admin_world):
    resp = client.patch(
        "/admin/tenants/tnt_user",
        headers=ADMIN_HEADERS,
        json={"plan": TenantPlan.PRO.value},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"] == TenantPlan.PRO.value


async def test_patch_tenant_duplicate_email_returns_409(client, admin_world):
    # tnt_host already has host@example.com — try to give tnt_user that email
    resp = client.patch(
        "/admin/tenants/tnt_user",
        headers=ADMIN_HEADERS,
        json={"owner_email": "tnt_host@example.com"},
    )
    assert resp.status_code == 409, resp.text


async def test_patch_tenant_404_when_missing(client, admin_world):
    resp = client.patch(
        "/admin/tenants/tnt_doesnotexist",
        headers=ADMIN_HEADERS,
        json={"name": "Ghost"},
    )
    assert resp.status_code == 404, resp.text
