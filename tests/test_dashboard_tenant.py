"""Tests for the per-tenant dashboard routes (D-Phase 5).

These cover the tenant pages under ``/dashboard/t/{tid}/...`` plus the
invite-accept landing under ``/dashboard/invites/accept/{token}``.

Pattern: build a small FastAPI app per test mounting just the routers we
need (dashboard, optionally auth) and seed the mongomock DB directly.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.dashboard import routes as dashboard_routes
from glossa.models.membership import Invite, TenantMember, TenantRole
from glossa.models.oauth_state import OAuthState
from glossa.models.session import Session
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.models.user import OAuthProvider, User
from glossa.oauth import flow as flow_mod
from glossa.oauth.base import OAuthUserInfo
from glossa.oauth.registry import reset_registry
from glossa.routes import auth as auth_routes

# --- Helpers ---------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults = {"base_url": "http://localhost:8200"}
    defaults.update(kwargs)
    return Settings(**defaults)


def _build_app(*, include_auth: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.settings = _make_settings()
    app.include_router(dashboard_routes.router)
    if include_auth:
        app.include_router(auth_routes.router)
    return app


async def _seed_user(
    db,
    *,
    user_id: str,
    email: str = "user@example.com",
    name: str = "User",
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
    session_id: str,
    user_id: str,
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
    tenant_id: str,
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
    member_id: str,
    tenant_id: str,
    user_id: str,
    role: TenantRole = TenantRole.MEMBER,
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


async def _seed_invite(
    db,
    *,
    invite_id: str = "inv_test01",
    tenant_id: str,
    token: str = "tkn-test-01",
    role: TenantRole = TenantRole.MEMBER,
    created_by_user_id: str = "usr_creator",
    expires_in_hours: int = 168,
    revoked: bool = False,
    accepted: bool = False,
    expired: bool = False,
) -> Invite:
    now = datetime.now(UTC)
    if expired:
        expires_at = now - timedelta(hours=1)
    else:
        expires_at = now + timedelta(hours=expires_in_hours)
    invite = Invite(
        id=invite_id,
        tenant_id=tenant_id,
        token=token,
        role=role,
        created_by_user_id=created_by_user_id,
        created_at=now,
        expires_at=expires_at,
        accepted_at=now if accepted else None,
        revoked_at=now if revoked else None,
    )
    await db.invites.insert_one(invite.model_dump())
    return invite


async def _seed_basic_actor(
    db,
    *,
    user_id: str,
    session_id: str,
    tenant_id: str,
    role: TenantRole = TenantRole.OWNER,
    email: str = "actor@example.com",
    name: str = "Actor",
    tenant_name: str = "Acme",
    member_id: str | None = None,
):
    """Seed a user + session + tenant + membership in one go."""
    await _seed_user(db, user_id=user_id, email=email, name=name)
    await _seed_session(db, session_id=session_id, user_id=user_id)
    await _seed_tenant(db, tenant_id=tenant_id, name=tenant_name, owner_email=email)
    await _seed_membership(
        db,
        member_id=member_id or f"mem_{user_id}",
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
    )


# --- Overview / members views --------------------------------------------------


async def test_tenant_overview_renders_for_member(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        tenant_name="Acme Inc",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/")
    assert resp.status_code == 200
    body = resp.text
    assert "Acme Inc" in body
    assert "tnt_acme" in body
    # Sidebar links to tenant-scoped pages now that current_tenant_id is set.
    assert "/dashboard/t/tnt_acme/members" in body
    assert "/dashboard/t/tnt_acme/invites" in body


async def test_tenant_overview_returns_404_for_non_member(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com")
    await _seed_session(mongomock_db, session_id="ses_bob", user_id="usr_bob")
    await _seed_tenant(mongomock_db, tenant_id="tnt_other", name="Other Co")
    client = TestClient(app, cookies={"glossa_session": "ses_bob"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_other/")
    assert resp.status_code == 404


async def test_tenant_members_lists_all_members_with_roles(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        email="alice@example.com",
        name="Alice",
        member_id="mem_alice",
    )
    # Second member
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com", name="Bob")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/members")
    assert resp.status_code == 200
    body = resp.text
    assert "Alice" in body
    assert "Bob" in body
    assert "alice@example.com" in body
    assert "bob@example.com" in body


async def test_tenant_members_shows_self_marker(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        email="alice@example.com",
        name="Alice",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/members")
    assert resp.status_code == 200
    assert "(you)" in resp.text


async def test_tenant_members_member_role_cannot_see_manage_controls(mongomock_db):
    app = _build_app()
    # A plain member viewing the members page should not see role-change selects or remove buttons.
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
        member_id="mem_alice",
    )
    # Seed someone else for comparison
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com", name="Bob")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.OWNER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/members")
    assert resp.status_code == 200
    body = resp.text
    # No role <select> on Bob's row, no remove form.
    assert "/dashboard/t/tnt_acme/members/mem_bob/role" not in body
    assert "/dashboard/t/tnt_acme/members/mem_bob/remove" not in body


# --- Role change ----------------------------------------------------------------


async def test_change_member_role_admin_can_change_other(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
        member_id="mem_admin",
    )
    # Need a second owner so the admin doesn't appear as the only manager — but unrelated to demotion.
    await _seed_user(mongomock_db, user_id="usr_owner", email="owner@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_owner",
        tenant_id="tnt_acme",
        user_id="usr_owner",
        role=TenantRole.OWNER,
    )
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/members/mem_bob/role",
        data={"role": "admin"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/members"
    updated = await mongomock_db.tenant_members.find_one({"id": "mem_bob"})
    assert updated["role"] == "admin"


async def test_change_member_role_member_cannot_change_anyone(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_member",
        session_id="ses_member",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
        member_id="mem_member",
    )
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_member"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/members/mem_bob/role",
        data={"role": "admin"},
    )
    assert resp.status_code == 403


async def test_change_member_role_cannot_demote_sole_owner(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_owner",
        session_id="ses_owner",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_owner",
    )
    # Another admin tries to demote the sole owner — must be blocked.
    await _seed_user(mongomock_db, user_id="usr_admin", email="admin@example.com")
    await _seed_session(mongomock_db, session_id="ses_admin", user_id="usr_admin")
    await _seed_membership(
        mongomock_db,
        member_id="mem_admin",
        tenant_id="tnt_acme",
        user_id="usr_admin",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/members/mem_owner/role",
        data={"role": "member"},
    )
    assert resp.status_code == 400
    # Unchanged in DB.
    doc = await mongomock_db.tenant_members.find_one({"id": "mem_owner"})
    assert doc["role"] == "owner"


async def test_change_member_role_invalid_role_400(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_admin",
    )
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/members/mem_bob/role",
        data={"role": "super-admin"},
    )
    assert resp.status_code == 400


# --- Member removal -------------------------------------------------------------


async def test_remove_member_admin_can_remove_other(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
        member_id="mem_admin",
    )
    # Owner so we don't trip the sole-owner guard
    await _seed_user(mongomock_db, user_id="usr_owner", email="owner@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_owner",
        tenant_id="tnt_acme",
        user_id="usr_owner",
        role=TenantRole.OWNER,
    )
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/members/mem_bob/remove")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/members"
    assert await mongomock_db.tenant_members.find_one({"id": "mem_bob"}) is None


async def test_remove_member_cannot_remove_sole_owner(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_owner",
        session_id="ses_owner",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_owner",
    )
    # Another admin tries to remove the sole owner.
    await _seed_user(mongomock_db, user_id="usr_admin", email="admin@example.com")
    await _seed_session(mongomock_db, session_id="ses_admin", user_id="usr_admin")
    await _seed_membership(
        mongomock_db,
        member_id="mem_admin",
        tenant_id="tnt_acme",
        user_id="usr_admin",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/members/mem_owner/remove")
    assert resp.status_code == 400
    # Owner still present.
    assert await mongomock_db.tenant_members.find_one({"id": "mem_owner"}) is not None


async def test_remove_member_member_cannot_remove(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_member",
        session_id="ses_member",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
        member_id="mem_member",
    )
    await _seed_user(mongomock_db, user_id="usr_bob", email="bob@example.com")
    await _seed_membership(
        mongomock_db,
        member_id="mem_bob",
        tenant_id="tnt_acme",
        user_id="usr_bob",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_member"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/members/mem_bob/remove")
    assert resp.status_code == 403
    assert await mongomock_db.tenant_members.find_one({"id": "mem_bob"}) is not None


# --- Invites: create / list / revoke -------------------------------------------


async def test_create_invite_admin_persists_row(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
        member_id="mem_admin",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/invites",
        data={"role": "member", "ttl_hours": "24"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/invites"
    invites = [doc async for doc in mongomock_db.invites.find({"tenant_id": "tnt_acme"})]
    assert len(invites) == 1
    inv = invites[0]
    assert inv["role"] == "member"
    assert inv["created_by_user_id"] == "usr_admin"
    assert inv["token"]
    assert inv["accepted_at"] is None
    assert inv["revoked_at"] is None


async def test_create_invite_member_cannot_create(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_member",
        session_id="ses_member",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
        member_id="mem_member",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_member"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/invites",
        data={"role": "member"},
    )
    assert resp.status_code == 403
    invites = [doc async for doc in mongomock_db.invites.find({})]
    assert invites == []


async def test_create_invite_invalid_role_400(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_admin",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/invites",
        data={"role": "godmode"},
    )
    assert resp.status_code == 400


async def test_create_invite_clamps_ttl_hours(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_admin",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)

    # Way too big: clamps to 720
    resp = client.post(
        "/dashboard/t/tnt_acme/invites",
        data={"role": "member", "ttl_hours": "99999"},
    )
    assert resp.status_code == 303

    # Way too small: clamps to 1
    resp = client.post(
        "/dashboard/t/tnt_acme/invites",
        data={"role": "member", "ttl_hours": "0"},
    )
    assert resp.status_code == 303

    invites = sorted(
        [doc async for doc in mongomock_db.invites.find({"tenant_id": "tnt_acme"})],
        key=lambda d: d["expires_at"],
    )
    assert len(invites) == 2
    now = datetime.now(UTC)
    # Lower clamp ≈ 1h
    short_expires = invites[0]["expires_at"]
    if short_expires.tzinfo is None:
        short_expires = short_expires.replace(tzinfo=UTC)
    assert short_expires - now < timedelta(hours=2)
    # Upper clamp ≈ 720h
    long_expires = invites[1]["expires_at"]
    if long_expires.tzinfo is None:
        long_expires = long_expires.replace(tzinfo=UTC)
    assert timedelta(hours=719) < long_expires - now <= timedelta(hours=720)


async def test_list_invites_only_shows_active(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_admin",
    )
    await _seed_invite(
        mongomock_db,
        invite_id="inv_active",
        tenant_id="tnt_acme",
        token="tkn-active",
    )
    await _seed_invite(
        mongomock_db,
        invite_id="inv_revoked",
        tenant_id="tnt_acme",
        token="tkn-revoked",
        revoked=True,
    )
    await _seed_invite(
        mongomock_db,
        invite_id="inv_accepted",
        tenant_id="tnt_acme",
        token="tkn-accepted",
        accepted=True,
    )
    await _seed_invite(
        mongomock_db,
        invite_id="inv_expired",
        tenant_id="tnt_acme",
        token="tkn-expired",
        expired=True,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/invites")
    assert resp.status_code == 200
    body = resp.text
    assert "tkn-active" in body
    assert "tkn-revoked" not in body
    assert "tkn-accepted" not in body
    assert "tkn-expired" not in body


async def test_revoke_invite_sets_revoked_at(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
        member_id="mem_admin",
    )
    await _seed_invite(
        mongomock_db,
        invite_id="inv_revokeable",
        tenant_id="tnt_acme",
        token="tkn-revokeable",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/invites/inv_revokeable/revoke")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/invites"
    doc = await mongomock_db.invites.find_one({"id": "inv_revokeable"})
    assert doc["revoked_at"] is not None


# --- Invite accept --------------------------------------------------------------


async def test_invite_accept_unauthenticated_shows_provider_buttons(mongomock_db):
    app = _build_app()
    await _seed_tenant(mongomock_db, tenant_id="tnt_acme", name="Acme")
    await _seed_invite(
        mongomock_db,
        invite_id="inv_open",
        tenant_id="tnt_acme",
        token="tkn-open",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard/invites/accept/tkn-open")
    assert resp.status_code == 200
    body = resp.text
    # Both encoded (%2F) and unencoded (/) representations are valid query values;
    # jinja's `urlencode` keeps slashes unescaped, which is fine for browsers.
    assert "/auth/google/start?redirect_to=" in body
    assert "/auth/github/start?redirect_to=" in body
    assert "/dashboard/invites/accept/tkn-open" in body
    # Shows the invited role
    assert "admin" in body


async def test_invite_accept_authenticated_creates_membership_and_redirects(mongomock_db):
    app = _build_app()
    # Tenant exists; invited user is signed in but not yet a member.
    await _seed_user(mongomock_db, user_id="usr_invitee", email="invitee@example.com")
    await _seed_session(mongomock_db, session_id="ses_invitee", user_id="usr_invitee")
    await _seed_tenant(mongomock_db, tenant_id="tnt_acme", name="Acme")
    await _seed_invite(
        mongomock_db,
        invite_id="inv_open",
        tenant_id="tnt_acme",
        token="tkn-open",
        role=TenantRole.MEMBER,
    )
    client = TestClient(
        app,
        cookies={"glossa_session": "ses_invitee"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard/invites/accept/tkn-open")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/"
    # Membership created
    member = await mongomock_db.tenant_members.find_one({"tenant_id": "tnt_acme", "user_id": "usr_invitee"})
    assert member is not None
    assert member["role"] == "member"
    # Invite marked accepted
    inv = await mongomock_db.invites.find_one({"id": "inv_open"})
    assert inv["accepted_at"] is not None


async def test_invite_accept_idempotent_when_already_member(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_invitee", email="invitee@example.com")
    await _seed_session(mongomock_db, session_id="ses_invitee", user_id="usr_invitee")
    await _seed_tenant(mongomock_db, tenant_id="tnt_acme")
    await _seed_membership(
        mongomock_db,
        member_id="mem_invitee",
        tenant_id="tnt_acme",
        user_id="usr_invitee",
        role=TenantRole.OWNER,
    )
    await _seed_invite(
        mongomock_db,
        invite_id="inv_open",
        tenant_id="tnt_acme",
        token="tkn-open",
        role=TenantRole.MEMBER,
    )
    client = TestClient(
        app,
        cookies={"glossa_session": "ses_invitee"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard/invites/accept/tkn-open")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/"
    # Still exactly one membership for this user/tenant.
    members = [m async for m in mongomock_db.tenant_members.find({"tenant_id": "tnt_acme", "user_id": "usr_invitee"})]
    assert len(members) == 1
    # Existing OWNER role was preserved (not overwritten by invite role).
    assert members[0]["role"] == "owner"


async def test_invite_accept_expired_returns_410(mongomock_db):
    app = _build_app()
    await _seed_tenant(mongomock_db, tenant_id="tnt_acme")
    await _seed_invite(
        mongomock_db,
        invite_id="inv_x",
        tenant_id="tnt_acme",
        token="tkn-expired",
        expired=True,
    )
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard/invites/accept/tkn-expired")
    assert resp.status_code == 410
    assert "expired" in resp.text.lower()


async def test_invite_accept_revoked_returns_410(mongomock_db):
    app = _build_app()
    await _seed_tenant(mongomock_db, tenant_id="tnt_acme")
    await _seed_invite(
        mongomock_db,
        invite_id="inv_x",
        tenant_id="tnt_acme",
        token="tkn-revoked",
        revoked=True,
    )
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard/invites/accept/tkn-revoked")
    assert resp.status_code == 410
    assert "revoked" in resp.text.lower()


async def test_invite_accept_unknown_returns_404(mongomock_db):
    app = _build_app()
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard/invites/accept/no-such-token")
    assert resp.status_code == 404


# --- OAuth callback redirect_to handling ---------------------------------------


class _FakeStrategy:
    """Stand-in OAuth strategy that returns canned userinfo with no HTTP."""

    def __init__(self, provider: OAuthProvider, userinfo: OAuthUserInfo) -> None:
        self.provider = provider
        self.client_id = "test-client"
        self.client_secret = "test-secret"
        self.authorize_url = "https://fake/authorize"
        self.token_url = "https://fake/token"
        self.userinfo_url = "https://fake/userinfo"
        self.scope = "openid email"
        self._userinfo = userinfo

    async def fetch_userinfo(self, client, access_token):
        return self._userinfo


@pytest.fixture(autouse=False)
def _reset_oauth_registry():
    reset_registry()
    yield
    reset_registry()


async def _seed_oauth_state(
    db,
    *,
    state_id: str,
    redirect_to: str | None,
    provider: OAuthProvider = OAuthProvider.GOOGLE,
) -> OAuthState:
    now = datetime.now(UTC)
    state = OAuthState(
        id=state_id,
        provider=provider,
        code_verifier="v",
        redirect_to=redirect_to,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    await db.oauth_states.insert_one(state.model_dump())
    return state


def _oauth_settings(**kwargs) -> Settings:
    defaults = {
        "google_oauth_client_id": "google-client-id",
        "google_oauth_client_secret": "google-client-secret",
        "github_oauth_client_id": "github-client-id",
        "github_oauth_client_secret": "github-client-secret",
        "base_url": "http://test.example",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _build_oauth_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(auth_routes.router)
    return app


async def test_oauth_callback_redirects_to_state_redirect_to_when_internal(
    mongomock_db, monkeypatch, _reset_oauth_registry
):
    from glossa.oauth import registry

    userinfo = OAuthUserInfo(
        provider_user_id="abc-1",
        email="alice@example.com",
        name="Alice",
        avatar_url=None,
    )
    registry._REGISTRY[OAuthProvider.GOOGLE] = _FakeStrategy(OAuthProvider.GOOGLE, userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_oauth_state(
        mongomock_db,
        state_id="state-internal",
        redirect_to="/dashboard/invites/accept/somet",
    )

    settings = _oauth_settings()
    app = _build_oauth_app(settings)
    client = TestClient(app)
    resp = client.get(
        "/auth/google/callback?code=auth-code&state=state-internal",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/invites/accept/somet"


async def test_oauth_callback_falls_back_to_dashboard_when_redirect_to_external(
    mongomock_db, monkeypatch, _reset_oauth_registry
):
    from glossa.oauth import registry

    userinfo = OAuthUserInfo(
        provider_user_id="abc-2",
        email="bob@example.com",
        name="Bob",
        avatar_url=None,
    )
    registry._REGISTRY[OAuthProvider.GOOGLE] = _FakeStrategy(OAuthProvider.GOOGLE, userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_oauth_state(
        mongomock_db,
        state_id="state-external",
        redirect_to="http://evil.com",
    )

    settings = _oauth_settings()
    app = _build_oauth_app(settings)
    client = TestClient(app)
    resp = client.get(
        "/auth/google/callback?code=auth-code&state=state-external",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/"


async def test_oauth_callback_falls_back_when_redirect_to_none(mongomock_db, monkeypatch, _reset_oauth_registry):
    from glossa.oauth import registry

    userinfo = OAuthUserInfo(
        provider_user_id="abc-3",
        email="carol@example.com",
        name="Carol",
        avatar_url=None,
    )
    registry._REGISTRY[OAuthProvider.GOOGLE] = _FakeStrategy(OAuthProvider.GOOGLE, userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_oauth_state(
        mongomock_db,
        state_id="state-none",
        redirect_to=None,
    )

    settings = _oauth_settings()
    app = _build_oauth_app(settings)
    client = TestClient(app)
    resp = client.get(
        "/auth/google/callback?code=auth-code&state=state-none",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/"


def test_resolve_post_login_redirect_helper():
    """Direct unit test for the safety check, independent of the full flow."""
    from glossa.routes.auth import _resolve_post_login_redirect

    assert _resolve_post_login_redirect(None) == "/dashboard/"
    assert _resolve_post_login_redirect("") == "/dashboard/"
    assert _resolve_post_login_redirect("/dashboard/t/foo/") == "/dashboard/t/foo/"
    assert _resolve_post_login_redirect("http://evil.com") == "/dashboard/"
    assert _resolve_post_login_redirect("https://evil.com/dashboard/") == "/dashboard/"
    # Protocol-relative — must fall back, otherwise //evil.com becomes the new host.
    assert _resolve_post_login_redirect("javascript:alert(1)") == "/dashboard/"
    # Path-only is fine
    assert _resolve_post_login_redirect("/dashboard/invites/accept/somet") == "/dashboard/invites/accept/somet"
