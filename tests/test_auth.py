"""Tests for the auth layer: AuthContext + Bearer dependency + system-mode bypass."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from glossa.auth.context import AuthContext
from glossa.auth.dependency import _extract_bearer, require_scope
from glossa.config import Settings
from glossa.main import app
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.storage.memory import InMemoryStorageBackend


def _make_settings(*, auth_required: bool = False, bootstrap: str | None = None) -> Settings:
    return Settings(auth_required=auth_required, bootstrap_admin_api_key=bootstrap)


@pytest.fixture
def app_client(mongomock_db):
    """A TestClient with app.state wired up like the lifespan would have.

    We deliberately do NOT enter the TestClient context manager, because that
    would run the real ``lifespan`` and try to talk to MinIO/Mongo. Instead we
    set ``app.state.settings`` and ``app.state.storage`` ourselves; the
    ``mongomock_db`` fixture already swapped the global DB client.
    """
    app.state.settings = _make_settings(auth_required=False)
    app.state.storage = InMemoryStorageBackend()
    return TestClient(app)


async def _seed_tenant(
    db,
    *,
    tenant_id: str = "tnt_abc",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    now = datetime.now(UTC)
    tenant = Tenant(
        id=tenant_id,
        name="Acme",
        owner_email=f"{tenant_id}@example.com",
        plan=TenantPlan.FREE,
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
    key_id: str = "key_abc123def456",
    scopes: list[Scope] | None = None,
    revoked: bool = False,
    created_at: datetime | None = None,
) -> ApiKey:
    created_at = created_at or datetime.now(UTC)
    api_key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        hashed_key=hash_key(plaintext),
        prefix=plaintext[: len("glsk_live_") + 8],
        scopes=scopes if scopes is not None else [Scope.SPACES_READ, Scope.SPACES_WRITE, Scope.QUERY],
        created_at=created_at,
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    await db.api_keys.insert_one(api_key.model_dump())
    return api_key


# --- Unit tests (no HTTP layer) -------------------------------------------------


def test_auth_context_has_scope_admin_passes():
    ctx = AuthContext.system()
    for scope in Scope:
        assert ctx.has_scope(scope) is True


def test_auth_context_has_scope_missing_returns_false():
    ctx = AuthContext(
        tenant_id="tnt_x",
        api_key_id="key_x",
        scopes=(Scope.SPACES_READ,),
        is_system=False,
    )
    assert ctx.has_scope(Scope.SPACES_READ) is True
    assert ctx.has_scope(Scope.ADMIN) is False
    assert ctx.has_scope(Scope.SPACES_WRITE) is False


def test_auth_context_system_factory_sets_flags():
    ctx = AuthContext.system()
    assert ctx.is_system is True
    assert ctx.api_key_id is None
    assert ctx.tenant_id == "_system"
    assert set(ctx.scopes) == set(Scope)


async def test_require_scope_rejects_missing_scope():
    checker = require_scope(Scope.ADMIN)
    ctx = AuthContext(
        tenant_id="tnt_x",
        api_key_id="key_x",
        scopes=(Scope.SPACES_READ,),
        is_system=False,
    )
    with pytest.raises(HTTPException) as exc_info:
        await checker(ctx=ctx)
    assert exc_info.value.status_code == 403
    assert "missing scope" in exc_info.value.detail


async def test_require_scope_allows_when_scope_present():
    checker = require_scope(Scope.SPACES_READ)
    ctx = AuthContext(
        tenant_id="tnt_x",
        api_key_id="key_x",
        scopes=(Scope.SPACES_READ,),
        is_system=False,
    )
    result = await checker(ctx=ctx)
    assert result is ctx


async def test_require_scope_allows_system_context():
    checker = require_scope(Scope.ADMIN)
    ctx = AuthContext.system()
    result = await checker(ctx=ctx)
    assert result is ctx


def test_extract_bearer_handles_lowercase_bearer():
    assert _extract_bearer("bearer xxx") == "xxx"
    assert _extract_bearer("Bearer xxx") == "xxx"
    assert _extract_bearer("BEARER xxx") == "xxx"


def test_extract_bearer_returns_none_for_missing_or_malformed():
    assert _extract_bearer(None) is None
    assert _extract_bearer("") is None
    assert _extract_bearer("notabearer xyz") is None
    assert _extract_bearer("Bearer") is None
    assert _extract_bearer("Bearer ") is None


# --- Self-host mode (auth_required=False) ---------------------------------------


def test_no_header_returns_system_context(app_client):
    """No Authorization header and auth_required=False -> request goes through."""
    app.state.settings = _make_settings(auth_required=False)
    resp = app_client.get("/spaces")
    assert resp.status_code == 200, resp.text


def test_invalid_bearer_token_returns_401(app_client):
    """Even with auth_required=False, a bad bearer token is rejected.

    Rationale: a token sent in the header is a positive claim of identity.
    Self-host mode only relaxes the absence-of-header case.
    """
    app.state.settings = _make_settings(auth_required=False)
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_bogus"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid api key"


def test_bootstrap_admin_token_returns_system_context(app_client):
    """A request carrying the bootstrap admin token is treated as a system context."""
    app.state.settings = _make_settings(auth_required=False, bootstrap="boot_xxx_super_secret")
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer boot_xxx_super_secret"},
    )
    assert resp.status_code == 200, resp.text


def test_bootstrap_admin_token_works_when_auth_required(app_client):
    """The bootstrap token bypasses DB lookup even when auth is required."""
    app.state.settings = _make_settings(auth_required=True, bootstrap="boot_xxx_super_secret")
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer boot_xxx_super_secret"},
    )
    assert resp.status_code == 200, resp.text


# --- auth_required=True ---------------------------------------------------------


def test_no_header_returns_401(app_client):
    app.state.settings = _make_settings(auth_required=True)
    resp = app_client.get("/spaces")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing Authorization header"


def test_malformed_authorization_returns_401(app_client):
    """A non-Bearer scheme is treated as no header -> 401 in strict mode."""
    app.state.settings = _make_settings(auth_required=True)
    resp = app_client.get("/spaces", headers={"Authorization": "notabearer xyz"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing Authorization header"


async def test_valid_api_key_returns_200(app_client, mongomock_db):
    app.state.settings = _make_settings(auth_required=True)
    await _seed_tenant(mongomock_db, tenant_id="tnt_valid")
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_test",
        tenant_id="tnt_valid",
        key_id="key_valid",
    )
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_test"},
    )
    assert resp.status_code == 200, resp.text


def test_unknown_api_key_returns_401(app_client):
    app.state.settings = _make_settings(auth_required=True)
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_unknown_does_not_exist"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid api key"


async def test_revoked_api_key_returns_401(app_client, mongomock_db):
    app.state.settings = _make_settings(auth_required=True)
    await _seed_tenant(mongomock_db, tenant_id="tnt_rev")
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_revoked",
        tenant_id="tnt_rev",
        key_id="key_revoked",
        revoked=True,
    )
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_revoked"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid api key"


async def test_suspended_tenant_returns_403(app_client, mongomock_db):
    app.state.settings = _make_settings(auth_required=True)
    await _seed_tenant(mongomock_db, tenant_id="tnt_susp", status=TenantStatus.SUSPENDED)
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_susp",
        tenant_id="tnt_susp",
        key_id="key_susp",
    )
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_susp"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tenant suspended"


async def test_deleted_tenant_returns_403(app_client, mongomock_db):
    app.state.settings = _make_settings(auth_required=True)
    await _seed_tenant(mongomock_db, tenant_id="tnt_del", status=TenantStatus.DELETED)
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_del",
        tenant_id="tnt_del",
        key_id="key_del",
    )
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_del"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tenant suspended"


async def test_tenant_missing_returns_401(app_client, mongomock_db):
    """API key exists but its tenant row was deleted/never created."""
    app.state.settings = _make_settings(auth_required=True)
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_orphan",
        tenant_id="tnt_orphan_missing",
        key_id="key_orphan",
    )
    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_orphan"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid api key"


async def test_last_used_at_is_updated(app_client, mongomock_db):
    app.state.settings = _make_settings(auth_required=True)
    created_at = datetime.now(UTC) - timedelta(hours=1)
    await _seed_tenant(mongomock_db, tenant_id="tnt_lu")
    await _seed_api_key(
        mongomock_db,
        plaintext="glsk_live_lu",
        tenant_id="tnt_lu",
        key_id="key_lu",
        created_at=created_at,
    )

    before = await mongomock_db.api_keys.find_one({"id": "key_lu"})
    assert before["last_used_at"] is None

    resp = app_client.get(
        "/spaces",
        headers={"Authorization": "Bearer glsk_live_lu"},
    )
    assert resp.status_code == 200, resp.text

    after = await mongomock_db.api_keys.find_one({"id": "key_lu"})
    assert after["last_used_at"] is not None
    # mongomock strips tzinfo on round-trip; compare in UTC-naive terms.
    last_used = after["last_used_at"]
    if last_used.tzinfo is None:
        last_used = last_used.replace(tzinfo=UTC)
    assert last_used > created_at


# --- Healthz stays public -------------------------------------------------------


def test_healthz_is_public_without_header(app_client):
    """/healthz is registered directly on the app, not on a router with auth deps."""
    app.state.settings = _make_settings(auth_required=True)
    resp = app_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
