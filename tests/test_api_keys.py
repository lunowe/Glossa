"""Tests for the API-key management routes (/tenants/{tid}/api-keys ...).

Mirrors the pattern in ``tests/test_admin.py`` — we build a fresh FastAPI
app per test and include the relevant router(s). The auth dependency runs
against the autouse mongomock DB fixture from ``conftest.py``.
"""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from glossa.auth import get_auth_context
from glossa.config import Settings
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.routes.api_keys import router as api_keys_router
from glossa.routes.spaces import router as spaces_router
from glossa.storage.memory import InMemoryStorageBackend

ALICE_TOKEN = "glsk_live_alice_secret"
BOB_TOKEN = "glsk_live_bob_secret"
ADMIN_TOKEN = "glsk_live_admin_secret"

ALICE_HEADERS = {"Authorization": f"Bearer {ALICE_TOKEN}"}
BOB_HEADERS = {"Authorization": f"Bearer {BOB_TOKEN}"}
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings() -> Settings:
    return Settings(auth_required=True)


@pytest.fixture
def app():
    """Build a fresh FastAPI app wired with the api-keys router (and spaces, for the
    revoked-key-can't-authenticate test that calls GET /spaces)."""
    app = FastAPI()
    app.state.settings = _settings()
    app.state.storage = InMemoryStorageBackend()
    app.include_router(api_keys_router, dependencies=[Depends(get_auth_context)])
    app.include_router(spaces_router, dependencies=[Depends(get_auth_context)])
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
async def world(mongomock_db):
    """Alice (regular tenant), Bob (regular tenant), Admin (admin key under tnt_admin)."""
    await _seed_tenant(mongomock_db, tenant_id="tnt_alice")
    await _seed_tenant(mongomock_db, tenant_id="tnt_bob")
    await _seed_tenant(mongomock_db, tenant_id="tnt_admin")
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
    await _seed_api_key(
        mongomock_db,
        plaintext=ADMIN_TOKEN,
        tenant_id="tnt_admin",
        key_id="key_admin",
        scopes=[Scope.ADMIN],
    )


# --- POST /tenants/{tid}/api-keys -----------------------------------------------


async def test_issue_key_returns_plaintext_once(client, world):
    resp = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        json={"label": "ci"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "plaintext" in body
    assert "api_key" in body
    plaintext = body["plaintext"]
    assert plaintext.startswith("glsk_live_")
    # Verify that the stored hashed_key matches sha256(plaintext)
    assert body["api_key"]["hashed_key"] == hash_key(plaintext)
    assert body["api_key"]["prefix"].startswith("glsk_live_")
    assert body["api_key"]["tenant_id"] == "tnt_alice"
    assert body["api_key"]["label"] == "ci"


async def test_issue_key_default_scopes_excludes_admin(client, world):
    resp = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        json={},
    )
    assert resp.status_code == 200, resp.text
    scopes = resp.json()["api_key"]["scopes"]
    assert Scope.ADMIN.value not in scopes
    # Default set includes spaces:read at minimum
    assert Scope.SPACES_READ.value in scopes


async def test_issue_key_custom_scopes(client, world):
    resp = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        json={"scopes": [Scope.SPACES_READ.value, Scope.QUERY.value]},
    )
    assert resp.status_code == 200, resp.text
    scopes = resp.json()["api_key"]["scopes"]
    assert set(scopes) == {Scope.SPACES_READ.value, Scope.QUERY.value}


async def test_issue_key_tenant_404(client, world):
    """Admin issuing a key for a tenant that doesn't exist."""
    resp = client.post(
        "/tenants/tnt_doesnotexist/api-keys",
        headers=ADMIN_HEADERS,
        json={},
    )
    assert resp.status_code == 404, resp.text


async def test_issue_key_as_tenant_self(client, world):
    resp = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        json={},
    )
    assert resp.status_code == 200, resp.text


async def test_issue_key_as_other_tenant_404(client, world):
    """Alice cannot manage Bob's keys — and the 404 doesn't leak Bob's existence."""
    resp = client.post(
        "/tenants/tnt_bob/api-keys",
        headers=ALICE_HEADERS,
        json={},
    )
    assert resp.status_code == 404, resp.text


async def test_issue_key_as_admin_for_other_tenant_works(client, world):
    resp = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ADMIN_HEADERS,
        json={"label": "issued-by-admin"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["api_key"]["tenant_id"] == "tnt_alice"
    assert body["api_key"]["label"] == "issued-by-admin"


# --- GET /tenants/{tid}/api-keys -----------------------------------------------


async def test_list_keys_excludes_revoked_by_default(client, world, mongomock_db):
    # Seed two extra keys for tnt_alice — one revoked
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_alive",
        tenant_id="tnt_alice",
        key_id="key_alice_alive",
    )
    revoked = ApiKey(
        id="key_alice_revoked",
        tenant_id="tnt_alice",
        hashed_key=hash_key("glsk_live_revoked"),
        prefix="glsk_live_revoked"[: len("glsk_live_") + 8],
        scopes=[Scope.SPACES_READ],
        created_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
    )
    await mongomock_db.api_keys.insert_one(revoked.model_dump())

    resp = client.get("/tenants/tnt_alice/api-keys", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert "key_alice_alive" in ids
    assert "key_alice_revoked" not in ids


async def test_list_keys_include_revoked_true(client, world, mongomock_db):
    revoked = ApiKey(
        id="key_alice_revoked",
        tenant_id="tnt_alice",
        hashed_key=hash_key("glsk_live_revoked"),
        prefix="glsk_live_revoked"[: len("glsk_live_") + 8],
        scopes=[Scope.SPACES_READ],
        created_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
    )
    await mongomock_db.api_keys.insert_one(revoked.model_dump())

    resp = client.get(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        params={"include_revoked": "true"},
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert "key_alice_revoked" in ids


async def test_list_keys_never_returns_plaintext(client, world):
    # Issue a key first so there's at least one in the list
    issued = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        json={},
    )
    assert issued.status_code == 200, issued.text
    resp = client.get("/tenants/tnt_alice/api-keys", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    for row in resp.json():
        assert "plaintext" not in row


# --- DELETE /tenants/{tid}/api-keys/{kid} ---------------------------------------


async def test_revoke_key_sets_revoked_at(client, world, mongomock_db):
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_to_revoke",
        tenant_id="tnt_alice",
        key_id="key_to_revoke",
    )
    resp = client.delete(
        "/tenants/tnt_alice/api-keys/key_to_revoke",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked_at"] is not None


async def test_revoke_key_idempotent(client, world, mongomock_db):
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_idem",
        tenant_id="tnt_alice",
        key_id="key_idem",
    )
    first = client.delete(
        "/tenants/tnt_alice/api-keys/key_idem",
        headers=ALICE_HEADERS,
    )
    assert first.status_code == 200, first.text
    first_revoked_at = first.json()["revoked_at"]
    assert first_revoked_at is not None

    second = client.delete(
        "/tenants/tnt_alice/api-keys/key_idem",
        headers=ALICE_HEADERS,
    )
    assert second.status_code == 200, second.text
    # Idempotent: revoked_at must not have changed.
    assert second.json()["revoked_at"] == first_revoked_at


async def test_revoke_key_404(client, world):
    resp = client.delete(
        "/tenants/tnt_alice/api-keys/key_does_not_exist",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 404, resp.text


async def test_revoke_key_wrong_tenant_404(client, world, mongomock_db):
    """Alice tries to revoke Bob's key — must be 404 (don't leak existence)."""
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_bob_owns_this",
        tenant_id="tnt_bob",
        key_id="key_bob_owned",
    )
    resp = client.delete(
        "/tenants/tnt_alice/api-keys/key_bob_owned",
        headers=ALICE_HEADERS,
    )
    # _authorize lets Alice in (she's calling /tenants/tnt_alice/...),
    # but the key doesn't belong to tnt_alice -> 404 from the route.
    assert resp.status_code == 404, resp.text


async def test_revoked_key_cannot_authenticate(client, world):
    """Issue a key, revoke it, then prove it can no longer authenticate."""
    issued = client.post(
        "/tenants/tnt_alice/api-keys",
        headers=ALICE_HEADERS,
        json={"label": "ephemeral"},
    )
    assert issued.status_code == 200, issued.text
    plaintext = issued.json()["plaintext"]
    key_id = issued.json()["api_key"]["id"]

    # The new key authenticates successfully
    ok = client.get("/spaces", headers={"Authorization": f"Bearer {plaintext}"})
    assert ok.status_code == 200, ok.text

    revoke = client.delete(
        f"/tenants/tnt_alice/api-keys/{key_id}",
        headers=ALICE_HEADERS,
    )
    assert revoke.status_code == 200, revoke.text

    # After revoke: 401
    after = client.get("/spaces", headers={"Authorization": f"Bearer {plaintext}"})
    assert after.status_code == 401, after.text
    assert after.json()["detail"] == "invalid api key"
