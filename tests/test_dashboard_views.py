"""Tests for the D-Phase 6 dashboard views: API keys, activity, quotas.

Pattern mirrors ``tests/test_dashboard_tenant.py`` — a small FastAPI app
per test that mounts the dashboard router, seeding the mongomock DB
directly. The autouse ``mongomock_db`` fixture comes from ``conftest.py``.
"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.dashboard import routes as dashboard_routes
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.membership import TenantMember, TenantRole
from glossa.models.session import Session
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.models.user import User
from glossa.usage.models import TenantQuota

# --- Helpers -------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults = {"base_url": "http://localhost:8200"}
    defaults.update(kwargs)
    return Settings(**defaults)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = _make_settings()
    app.include_router(dashboard_routes.router)
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


async def _seed_session(db, *, session_id: str, user_id: str) -> Session:
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
) -> None:
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


async def _seed_api_key(
    db,
    *,
    key_id: str,
    tenant_id: str,
    plaintext: str = "glsk_live_dummy_xxxxxxxxxxxxxxxxxxxxxxxx",
    label: str | None = None,
    scopes: list[Scope] | None = None,
    revoked: bool = False,
) -> ApiKey:
    now = datetime.now(UTC)
    key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        hashed_key=hash_key(plaintext),
        prefix=plaintext[: len("glsk_live_") + 8],
        label=label,
        scopes=scopes if scopes is not None else [Scope.SPACES_READ, Scope.QUERY],
        created_at=now,
        revoked_at=now if revoked else None,
    )
    await db.api_keys.insert_one(key.model_dump())
    return key


async def _seed_request_event(
    db,
    *,
    event_id: str,
    tenant_id: str,
    method: str = "GET",
    path: str = "/spaces",
    status_code: int = 200,
    duration_ms: int = 5,
    api_key_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    doc = {
        "id": event_id,
        "tenant_id": tenant_id,
        "api_key_id": api_key_id,
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "created_at": created_at or datetime.now(UTC),
        "error": None,
    }
    await db.request_events.insert_one(doc)


# --- API keys page -------------------------------------------------------------


async def test_keys_page_lists_existing_keys(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
    )
    await _seed_api_key(
        mongomock_db,
        key_id="key_aaa",
        tenant_id="tnt_acme",
        plaintext="glsk_live_first_keyaaaaaaaaaaaaaaaaaa",
        label="production",
    )
    await _seed_api_key(
        mongomock_db,
        key_id="key_bbb",
        tenant_id="tnt_acme",
        plaintext="glsk_live_secnd_keybbbbbbbbbbbbbbbbbb",
        label="staging",
        revoked=True,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/keys")
    assert resp.status_code == 200
    body = resp.text
    assert "production" in body
    assert "staging" in body
    # Prefix of one of the keys
    assert "glsk_live_first_ke" in body or "glsk_live_first" in body
    assert "revoked" in body
    assert "active" in body


async def test_keys_page_member_cannot_see_issue_form(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/keys")
    assert resp.status_code == 200
    body = resp.text
    # The issue-form post action is gated by can_manage.
    assert '<form method="post" action="/dashboard/t/tnt_acme/keys">' not in body


async def test_issue_key_admin_inserts_row_and_redirects_with_plaintext_in_query(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/keys",
        data={"label": "prod"},
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/dashboard/t/tnt_acme/keys?")
    assert "new_plaintext=glsk_live_" in location
    assert "new_prefix=glsk_live_" in location
    docs = [doc async for doc in mongomock_db.api_keys.find({"tenant_id": "tnt_acme"})]
    assert len(docs) == 1
    assert docs[0]["label"] == "prod"
    assert docs[0]["revoked_at"] is None
    # Plaintext is never stored — only hash + prefix.
    assert "hashed_key" in docs[0]
    assert docs[0]["hashed_key"]


async def test_issue_key_default_scopes_when_none_selected(mongomock_db):
    from glossa.models.api_key import DEFAULT_SCOPES

    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/keys", data={"label": "no-scopes"})
    assert resp.status_code == 303
    docs = [doc async for doc in mongomock_db.api_keys.find({"tenant_id": "tnt_acme"})]
    assert len(docs) == 1
    persisted = [Scope(s) for s in docs[0]["scopes"]]
    assert persisted == list(DEFAULT_SCOPES)


async def test_issue_key_custom_scopes_persisted(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/keys",
        data={"label": "tight", "scopes": ["spaces:read", "query"]},
    )
    assert resp.status_code == 303
    docs = [doc async for doc in mongomock_db.api_keys.find({"tenant_id": "tnt_acme"})]
    assert len(docs) == 1
    assert set(docs[0]["scopes"]) == {"spaces:read", "query"}


async def test_issue_key_invalid_scope_400(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/keys",
        data={"label": "bad", "scopes": ["not-a-scope"]},
    )
    assert resp.status_code == 400
    docs = [doc async for doc in mongomock_db.api_keys.find({"tenant_id": "tnt_acme"})]
    assert docs == []


async def test_issue_key_member_forbidden(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_member",
        session_id="ses_member",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_member"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/keys", data={"label": "should-fail"})
    assert resp.status_code == 403
    docs = [doc async for doc in mongomock_db.api_keys.find({"tenant_id": "tnt_acme"})]
    assert docs == []


async def test_revoke_key_admin_sets_revoked_at(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
    )
    await _seed_api_key(
        mongomock_db,
        key_id="key_to_revoke",
        tenant_id="tnt_acme",
        plaintext="glsk_live_revoke_xxxxxxxxxxxxxxxxxxxxx",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/keys/key_to_revoke/revoke")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/keys"
    doc = await mongomock_db.api_keys.find_one({"id": "key_to_revoke"})
    assert doc["revoked_at"] is not None


async def test_revoke_key_member_forbidden(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_member",
        session_id="ses_member",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    await _seed_api_key(
        mongomock_db,
        key_id="key_protected",
        tenant_id="tnt_acme",
        plaintext="glsk_live_protct_xxxxxxxxxxxxxxxxxxxxx",
    )
    client = TestClient(app, cookies={"glossa_session": "ses_member"}, follow_redirects=False)
    resp = client.post("/dashboard/t/tnt_acme/keys/key_protected/revoke")
    assert resp.status_code == 403
    doc = await mongomock_db.api_keys.find_one({"id": "key_protected"})
    assert doc["revoked_at"] is None


async def test_keys_page_renders_one_time_plaintext_when_query_param_present(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    plaintext = "glsk_live_freshly_issued_token_value_here_xx"
    resp = client.get(f"/dashboard/t/tnt_acme/keys?new_plaintext={plaintext}&new_prefix=glsk_live_freshly")
    assert resp.status_code == 200
    body = resp.text
    assert "<code" in body
    assert plaintext in body


# --- Activity page -------------------------------------------------------------


async def test_activity_page_renders_summary_and_events(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    await _seed_request_event(
        mongomock_db,
        event_id="ev_1",
        tenant_id="tnt_acme",
        method="GET",
        path="/spaces",
        status_code=200,
    )
    await _seed_request_event(
        mongomock_db,
        event_id="ev_2",
        tenant_id="tnt_acme",
        method="POST",
        path="/spaces/abc/sources",
        status_code=500,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/activity")
    assert resp.status_code == 200
    body = resp.text
    # Summary numbers
    assert "Last 24h" in body
    # Both event rows
    assert "/spaces" in body
    assert "/spaces/abc/sources" in body
    # Status codes appear
    assert "200" in body
    assert "500" in body


async def test_activity_page_filters_by_method(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    await _seed_request_event(mongomock_db, event_id="ev_get", tenant_id="tnt_acme", method="GET", path="/spaces")
    await _seed_request_event(mongomock_db, event_id="ev_post", tenant_id="tnt_acme", method="POST", path="/spaces")
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/activity?method=POST")
    assert resp.status_code == 200
    body = resp.text
    # Only the POST row should appear; we can detect via the method column cell.
    assert "<code>POST</code>" in body
    assert "<code>GET</code>" not in body


async def test_activity_page_filters_by_path_prefix(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    await _seed_request_event(mongomock_db, event_id="ev_spaces", tenant_id="tnt_acme", path="/spaces")
    await _seed_request_event(mongomock_db, event_id="ev_query", tenant_id="tnt_acme", path="/query")
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/activity?path_prefix=/query")
    assert resp.status_code == 200
    body = resp.text
    assert "<code>/query</code>" in body
    assert "<code>/spaces</code>" not in body


async def test_activity_page_filters_by_status_min(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    await _seed_request_event(mongomock_db, event_id="ev_ok", tenant_id="tnt_acme", path="/spaces/ok", status_code=200)
    await _seed_request_event(
        mongomock_db,
        event_id="ev_err",
        tenant_id="tnt_acme",
        path="/spaces/broken",
        status_code=500,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/activity?status_min=400")
    assert resp.status_code == 200
    body = resp.text
    assert "/spaces/broken" in body
    assert "/spaces/ok" not in body


async def test_activity_page_hours_parameter_defaults_to_24(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_alice",
        session_id="ses_alice",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_alice"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/activity")
    assert resp.status_code == 200
    assert "Last 24h" in resp.text


# --- Quotas page ---------------------------------------------------------------


async def test_quotas_page_renders_gauges_for_set_limits(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_owner",
        session_id="ses_owner",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
    )
    quota = TenantQuota(
        tenant_id="tnt_acme",
        monthly_cost_limit_usd=10.0,
        monthly_token_limit=1_000_000,
        max_sources_per_space=50,
        max_storage_bytes=1024 * 1024,
        max_requests_per_minute=120,
        updated_at=datetime.now(UTC),
    )
    await mongomock_db.tenant_quotas.insert_one(quota.model_dump())
    client = TestClient(app, cookies={"glossa_session": "ses_owner"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/quotas")
    assert resp.status_code == 200
    body = resp.text
    assert "<progress" in body
    # Values from the quota appear somewhere in the page
    assert "1000000" in body
    assert "120" in body


async def test_quotas_page_renders_unlimited_when_no_quota_row(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_owner",
        session_id="ses_owner",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_owner"}, follow_redirects=False)
    resp = client.get("/dashboard/t/tnt_acme/quotas")
    assert resp.status_code == 200
    body = resp.text
    assert "unlimited" in body


async def test_update_quotas_admin_persists_all_six_dimensions(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.ADMIN,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/quotas",
        data={
            "monthly_cost_limit_usd": "25.50",
            "monthly_token_limit": "500000",
            "max_sources_per_space": "100",
            "max_storage_bytes": "1048576",
            "max_requests_per_minute": "60",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/t/tnt_acme/quotas"
    doc = await mongomock_db.tenant_quotas.find_one({"tenant_id": "tnt_acme"})
    assert doc is not None
    assert doc["monthly_cost_limit_usd"] == 25.50
    assert doc["monthly_token_limit"] == 500000
    assert doc["max_sources_per_space"] == 100
    assert doc["max_storage_bytes"] == 1048576
    assert doc["max_requests_per_minute"] == 60


async def test_update_quotas_member_forbidden(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_member",
        session_id="ses_member",
        tenant_id="tnt_acme",
        role=TenantRole.MEMBER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_member"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/quotas",
        data={"monthly_cost_limit_usd": "10.0"},
    )
    assert resp.status_code == 403
    assert await mongomock_db.tenant_quotas.find_one({"tenant_id": "tnt_acme"}) is None


async def test_update_quotas_blank_field_means_unlimited(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
    )
    # Pre-seed an existing limit.
    initial = TenantQuota(
        tenant_id="tnt_acme",
        monthly_cost_limit_usd=10.0,
        updated_at=datetime.now(UTC),
    )
    await mongomock_db.tenant_quotas.insert_one(initial.model_dump())
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/quotas",
        data={
            "monthly_cost_limit_usd": "",
            "monthly_token_limit": "",
            "max_sources_per_space": "",
            "max_storage_bytes": "",
            "max_requests_per_minute": "",
        },
    )
    assert resp.status_code == 303
    doc = await mongomock_db.tenant_quotas.find_one({"tenant_id": "tnt_acme"})
    assert doc is not None
    assert doc["monthly_cost_limit_usd"] is None
    assert doc["monthly_token_limit"] is None
    assert doc["max_sources_per_space"] is None
    assert doc["max_storage_bytes"] is None
    assert doc["max_requests_per_minute"] is None


async def test_update_quotas_invalid_int_400(mongomock_db):
    app = _build_app()
    await _seed_basic_actor(
        mongomock_db,
        user_id="usr_admin",
        session_id="ses_admin",
        tenant_id="tnt_acme",
        role=TenantRole.OWNER,
    )
    client = TestClient(app, cookies={"glossa_session": "ses_admin"}, follow_redirects=False)
    resp = client.post(
        "/dashboard/t/tnt_acme/quotas",
        data={"monthly_token_limit": "not-a-number"},
    )
    assert resp.status_code == 400
    assert await mongomock_db.tenant_quotas.find_one({"tenant_id": "tnt_acme"}) is None


# --- Shared access checks ------------------------------------------------------


async def test_non_member_404_on_all_three_pages(mongomock_db):
    """Foreign-tenant user OR anonymous request must 404/redirect on /keys,
    /activity, /quotas. Foreign user → 404 (membership leak avoidance);
    anonymous → redirect to /dashboard/login (require_session behavior)."""
    app = _build_app()
    # Foreign user is signed in but not a member of tnt_target.
    await _seed_user(mongomock_db, user_id="usr_foreign", email="foreign@example.com")
    await _seed_session(mongomock_db, session_id="ses_foreign", user_id="usr_foreign")
    await _seed_tenant(mongomock_db, tenant_id="tnt_target", name="Target")

    foreign_client = TestClient(
        app,
        cookies={"glossa_session": "ses_foreign"},
        follow_redirects=False,
    )
    for path in (
        "/dashboard/t/tnt_target/keys",
        "/dashboard/t/tnt_target/activity",
        "/dashboard/t/tnt_target/quotas",
    ):
        resp = foreign_client.get(path)
        assert resp.status_code == 404, f"foreign user should 404 on {path}, got {resp.status_code}"

    # Anonymous browser request (no session cookie + text/html accept) —
    # require_session redirects to /dashboard/login.
    anon_client = TestClient(app, follow_redirects=False)
    for path in (
        "/dashboard/t/tnt_target/keys",
        "/dashboard/t/tnt_target/activity",
        "/dashboard/t/tnt_target/quotas",
    ):
        resp = anon_client.get(path, headers={"Accept": "text/html"})
        assert resp.status_code == 303, f"anon should redirect on {path}, got {resp.status_code}"
        assert "/dashboard/login" in resp.headers["location"]
