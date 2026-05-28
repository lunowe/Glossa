"""Tests for the dashboard HTML scaffold (D-Phase 4).

These exercise just the dashboard router — Jinja2 templates rendered against a
minimal FastAPI app. The session layer (cookie -> SessionContext resolution) is
already covered by ``test_sessions.py``; here we only verify the dashboard's
own behavior: auth-gated redirects, listing tenants for the signed-in user,
and tenant-leakage isolation.

Pattern: build a tiny FastAPI app per test that mounts ``dashboard_routes.router``
and sets ``app.state.settings`` — the autouse ``mongomock_db`` fixture handles
the database client.
"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.dashboard import routes as dashboard_routes
from glossa.models.membership import TenantMember, TenantRole
from glossa.models.session import Session
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.models.user import User


def _make_settings(**kwargs) -> Settings:
    return Settings(**kwargs)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = _make_settings()
    app.include_router(dashboard_routes.router)
    return app


async def _seed_user(
    db,
    *,
    user_id: str = "usr_alice",
    email: str = "alice@example.com",
    name: str = "Alice",
) -> User:
    now = datetime.now(UTC)
    user = User(
        id=user_id,
        email=email,
        name=name,
        oauth_accounts=[],
        created_at=now,
        last_login_at=None,
    )
    await db.users.insert_one(user.model_dump())
    return user


async def _seed_session(
    db,
    *,
    session_id: str = "ses_cookie_value",
    user_id: str = "usr_alice",
) -> Session:
    now = datetime.now(UTC)
    session = Session(
        id=session_id,
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        last_seen_at=now,
        ip=None,
        user_agent=None,
    )
    await db.sessions.insert_one(session.model_dump())
    return session


async def _seed_tenant(
    db,
    *,
    tenant_id: str = "tnt_acme",
    name: str = "Acme",
    owner_email: str = "owner@example.com",
) -> Tenant:
    now = datetime.now(UTC)
    tenant = Tenant(
        id=tenant_id,
        name=name,
        owner_email=owner_email,
        plan=TenantPlan.FREE,
        status=TenantStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    await db.tenants.insert_one(tenant.model_dump())
    return tenant


async def _seed_membership(
    db,
    *,
    member_id: str = "mem_001",
    tenant_id: str,
    user_id: str,
    role: TenantRole = TenantRole.OWNER,
) -> TenantMember:
    now = datetime.now(UTC)
    member = TenantMember(
        id=member_id,
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        joined_at=now,
    )
    await db.tenant_members.insert_one(member.model_dump())
    return member


# --- /dashboard/login ----------------------------------------------------------


def test_login_page_renders_with_provider_buttons():
    app = _build_app()
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard/login")
    assert resp.status_code == 200
    body = resp.text
    assert 'href="/auth/google/start"' in body
    assert 'href="/auth/github/start"' in body


async def test_login_page_redirects_when_signed_in(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_signed_in")
    await _seed_session(
        mongomock_db,
        session_id="ses_already_signed_in",
        user_id="usr_signed_in",
    )

    client = TestClient(
        app,
        cookies={"glossa_session": "ses_already_signed_in"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/"


# --- /dashboard (require_session behavior) -------------------------------------


def test_dashboard_index_requires_session_browser():
    app = _build_app()
    client = TestClient(app, follow_redirects=False)
    resp = client.get(
        "/dashboard",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"


def test_dashboard_index_returns_401_for_htmx_unauth():
    app = _build_app()
    client = TestClient(app, follow_redirects=False)
    resp = client.get(
        "/dashboard",
        headers={"HX-Request": "true", "Accept": "text/html"},
    )
    assert resp.status_code == 401
    assert resp.headers["hx-redirect"] == "/dashboard/login"


# --- /dashboard listing behavior -----------------------------------------------


async def test_dashboard_index_lists_tenants_for_user(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_lister", email="lister@example.com")
    await _seed_session(
        mongomock_db,
        session_id="ses_lister",
        user_id="usr_lister",
    )
    await _seed_tenant(
        mongomock_db,
        tenant_id="tnt_lister",
        name="Lister Co",
        owner_email="lister@example.com",
    )
    await _seed_membership(
        mongomock_db,
        member_id="mem_lister",
        tenant_id="tnt_lister",
        user_id="usr_lister",
        role=TenantRole.OWNER,
    )

    client = TestClient(
        app,
        cookies={"glossa_session": "ses_lister"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "Lister Co" in body
    assert TenantRole.OWNER.value in body
    # And a link to the tenant detail page (D-Phase 5 will fill it in).
    assert "/dashboard/t/tnt_lister/" in body


async def test_dashboard_index_shows_empty_state_for_user_without_membership(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_lonely", email="lonely@example.com")
    await _seed_session(
        mongomock_db,
        session_id="ses_lonely",
        user_id="usr_lonely",
    )

    client = TestClient(
        app,
        cookies={"glossa_session": "ses_lonely"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "don't belong" in resp.text


async def test_dashboard_index_does_not_leak_other_users_tenants(mongomock_db):
    app = _build_app()
    # Alice + her tenant
    await _seed_user(mongomock_db, user_id="usr_alice", email="alice@example.com", name="Alice")
    await _seed_session(
        mongomock_db,
        session_id="ses_alice",
        user_id="usr_alice",
    )
    await _seed_tenant(
        mongomock_db,
        tenant_id="tnt_alice",
        name="Alice Workspace",
        owner_email="alice@example.com",
    )
    await _seed_membership(
        mongomock_db,
        member_id="mem_alice",
        tenant_id="tnt_alice",
        user_id="usr_alice",
        role=TenantRole.OWNER,
    )

    # Bob + his tenant — Alice must not see this
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com", name="Bob")
    await _seed_tenant(
        mongomock_db,
        tenant_id="tnt_bob",
        name="Bob Workspace",
        owner_email="bob@example.com",
    )
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_bob",
        user_id="usr_bob",
        role=TenantRole.OWNER,
    )

    client = TestClient(
        app,
        cookies={"glossa_session": "ses_alice"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "Alice Workspace" in body
    assert "tnt_alice" in body
    assert "Bob Workspace" not in body
    assert "tnt_bob" not in body
