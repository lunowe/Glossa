"""Tests for the OAuth code-flow layer (Google + GitHub).

The flow uses ``httpx`` to talk to the provider, so the unit tests inject a
fake strategy into the ``glossa.oauth.registry`` and monkeypatch
``_exchange_code`` so no real HTTP is performed.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.models.membership import TenantRole
from glossa.models.oauth_state import OAuthState
from glossa.models.session import Session
from glossa.models.user import OAuthAccount, OAuthProvider, User
from glossa.oauth import flow as flow_mod
from glossa.oauth.base import OAuthUserInfo
from glossa.oauth.flow import _pkce_pair, begin_oauth, complete_oauth
from glossa.oauth.registry import (
    get_strategy,
    register_default_strategies,
    reset_registry,
)
from glossa.routes import auth as auth_routes
from glossa.sessions import set_session_cookie

# --- Helpers --------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    """Default settings with both providers configured (override per-test)."""
    defaults = {
        "google_oauth_client_id": "google-client-id",
        "google_oauth_client_secret": "google-client-secret",
        "github_oauth_client_id": "github-client-id",
        "github_oauth_client_secret": "github-client-secret",
        "base_url": "http://test.example",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class FakeStrategy:
    """Stand-in strategy that returns canned userinfo without HTTP."""

    def __init__(
        self,
        provider: OAuthProvider,
        userinfo: OAuthUserInfo,
        *,
        client_id: str | None = "test-client",
        client_secret: str | None = "test-secret",
    ):
        self.provider = provider
        self.client_id = client_id
        self.client_secret = client_secret
        self.authorize_url = "https://fake-provider.test/authorize"
        self.token_url = "https://fake-provider.test/token"
        self.userinfo_url = "https://fake-provider.test/userinfo"
        self.scope = "openid email"
        self._userinfo = userinfo

    async def fetch_userinfo(self, client, access_token):
        return self._userinfo


@pytest.fixture(autouse=True)
def _reset_registry_between_tests():
    """Make sure registry state from one test doesn't leak into another."""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def fake_userinfo() -> OAuthUserInfo:
    return OAuthUserInfo(
        provider_user_id="abc-123",
        email="alice@example.com",
        name="Alice",
        avatar_url="https://example.com/a.png",
    )


def _build_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(auth_routes.router)
    return app


async def _seed_state(
    db,
    *,
    state_id: str = "state-fixed",
    provider: OAuthProvider = OAuthProvider.GOOGLE,
    code_verifier: str = "verifier-fixed",
    expires_at: datetime | None = None,
) -> OAuthState:
    now = datetime.now(UTC)
    state = OAuthState(
        id=state_id,
        provider=provider,
        code_verifier=code_verifier,
        redirect_to=None,
        created_at=now,
        expires_at=expires_at or (now + timedelta(minutes=10)),
    )
    await db.oauth_states.insert_one(state.model_dump())
    return state


# --- PKCE unit tests -------------------------------------------------------------


def test_pkce_pair_returns_verifier_and_challenge():
    verifier, challenge = _pkce_pair()
    # Reconstruct the challenge from the verifier and confirm S256 algorithm.
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_verifier_length_in_rfc_range():
    # RFC 7636: verifier must be 43-128 characters.
    for _ in range(10):
        verifier, _ = _pkce_pair()
        assert 43 <= len(verifier) <= 128


def test_pkce_challenge_url_safe_unpadded():
    _, challenge = _pkce_pair()
    # No padding, no plus or slash characters.
    assert "=" not in challenge
    assert "+" not in challenge
    assert "/" not in challenge


# --- begin_oauth tests -----------------------------------------------------------


async def test_begin_oauth_creates_state_row_and_returns_authorize_url(mongomock_db):
    settings = _make_settings()
    register_default_strategies(settings)

    result = await begin_oauth(provider=OAuthProvider.GOOGLE, settings=settings)

    # A state row exists with the expected id and provider.
    state_doc = await mongomock_db.oauth_states.find_one({"id": result.state_id})
    assert state_doc is not None
    assert state_doc["provider"] == OAuthProvider.GOOGLE.value
    assert state_doc["code_verifier"]

    # The redirect URL targets the strategy's authorize endpoint and includes
    # all required query params.
    assert result.authorize_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=google-client-id" in result.authorize_url
    assert "code_challenge_method=S256" in result.authorize_url
    assert "code_challenge=" in result.authorize_url
    assert "scope=openid+email+profile" in result.authorize_url
    assert f"state={result.state_id}" in result.authorize_url
    assert "redirect_uri=http%3A%2F%2Ftest.example%2Fauth%2Fgoogle%2Fcallback" in result.authorize_url


async def test_begin_oauth_records_redirect_to_in_state(mongomock_db):
    settings = _make_settings()
    register_default_strategies(settings)

    result = await begin_oauth(
        provider=OAuthProvider.GOOGLE,
        settings=settings,
        redirect_to="/dashboard/spaces",
    )
    state_doc = await mongomock_db.oauth_states.find_one({"id": result.state_id})
    assert state_doc["redirect_to"] == "/dashboard/spaces"


async def test_begin_oauth_raises_when_client_id_not_configured(mongomock_db):
    settings = _make_settings(google_oauth_client_id=None)
    register_default_strategies(settings)

    with pytest.raises(ValueError, match="client_id not configured"):
        await begin_oauth(provider=OAuthProvider.GOOGLE, settings=settings)


async def test_begin_oauth_github_endpoint_and_scope(mongomock_db):
    settings = _make_settings()
    register_default_strategies(settings)

    result = await begin_oauth(provider=OAuthProvider.GITHUB, settings=settings)
    assert result.authorize_url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=github-client-id" in result.authorize_url
    assert "scope=read%3Auser+user%3Aemail" in result.authorize_url


# --- complete_oauth tests -------------------------------------------------------


async def test_complete_oauth_creates_new_user_and_tenant(mongomock_db, fake_userinfo, monkeypatch):
    settings = _make_settings()
    # Inject a fake strategy so no HTTP is performed.
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GOOGLE] = FakeStrategy(OAuthProvider.GOOGLE, fake_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_state(mongomock_db, state_id="state-new-user")
    result = await complete_oauth(
        provider=OAuthProvider.GOOGLE,
        settings=settings,
        code="auth-code",
        state_id="state-new-user",
    )

    assert result.is_new_user is True
    assert result.user.email == "alice@example.com"
    assert result.user.name == "Alice"
    assert result.user.id.startswith("usr_")
    assert len(result.user.oauth_accounts) == 1
    acct = result.user.oauth_accounts[0]
    assert acct.provider == OAuthProvider.GOOGLE
    assert acct.provider_user_id == "abc-123"

    # The user row exists.
    user_doc = await mongomock_db.users.find_one({"id": result.user.id})
    assert user_doc is not None

    # An initial tenant + owner membership were auto-created.
    member_doc = await mongomock_db.tenant_members.find_one({"user_id": result.user.id})
    assert member_doc is not None
    assert member_doc["role"] == TenantRole.OWNER.value
    tenant_doc = await mongomock_db.tenants.find_one({"id": member_doc["tenant_id"]})
    assert tenant_doc is not None
    assert tenant_doc["owner_email"] == "alice@example.com"
    assert "Alice" in tenant_doc["name"]


async def test_complete_oauth_returns_existing_user_on_email_match(mongomock_db, fake_userinfo, monkeypatch):
    settings = _make_settings()
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GOOGLE] = FakeStrategy(OAuthProvider.GOOGLE, fake_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    # Pre-seed a user with the same email and a google account.
    now = datetime.now(UTC)
    existing = User(
        id="usr_existing0001",
        email="alice@example.com",
        name="Alice",
        oauth_accounts=[
            OAuthAccount(
                provider=OAuthProvider.GOOGLE,
                provider_user_id="abc-123",
                email="alice@example.com",
                linked_at=now,
            )
        ],
        created_at=now,
        last_login_at=None,
    )
    await mongomock_db.users.insert_one(existing.model_dump())

    await _seed_state(mongomock_db, state_id="state-existing-user")
    result = await complete_oauth(
        provider=OAuthProvider.GOOGLE,
        settings=settings,
        code="auth-code",
        state_id="state-existing-user",
    )

    assert result.is_new_user is False
    assert result.user.id == "usr_existing0001"
    # No new tenant was auto-created.
    tenants = [t async for t in mongomock_db.tenants.find({})]
    assert tenants == []
    members = [m async for m in mongomock_db.tenant_members.find({})]
    assert members == []
    # last_login_at was bumped.
    refreshed = await mongomock_db.users.find_one({"id": "usr_existing0001"})
    assert refreshed["last_login_at"] is not None


async def test_complete_oauth_appends_oauth_account_on_provider_link(mongomock_db, monkeypatch):
    """Existing user signed up via Google later signs in with GitHub: their
    user row gains a second OAuthAccount entry."""
    settings = _make_settings()

    now = datetime.now(UTC)
    existing = User(
        id="usr_dual0000001",
        email="alice@example.com",
        name="Alice",
        oauth_accounts=[
            OAuthAccount(
                provider=OAuthProvider.GOOGLE,
                provider_user_id="google-sub",
                email="alice@example.com",
                linked_at=now,
            )
        ],
        created_at=now,
        last_login_at=None,
    )
    await mongomock_db.users.insert_one(existing.model_dump())

    github_userinfo = OAuthUserInfo(
        provider_user_id="github-id-77",
        email="alice@example.com",
        name="Alice",
        avatar_url=None,
    )
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GITHUB] = FakeStrategy(OAuthProvider.GITHUB, github_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_state(mongomock_db, state_id="state-link", provider=OAuthProvider.GITHUB)
    result = await complete_oauth(
        provider=OAuthProvider.GITHUB,
        settings=settings,
        code="auth-code",
        state_id="state-link",
    )

    assert result.is_new_user is False
    assert result.user.id == "usr_dual0000001"
    assert {a.provider for a in result.user.oauth_accounts} == {
        OAuthProvider.GOOGLE,
        OAuthProvider.GITHUB,
    }
    github_acct = next(a for a in result.user.oauth_accounts if a.provider == OAuthProvider.GITHUB)
    assert github_acct.provider_user_id == "github-id-77"


async def test_complete_oauth_state_is_single_use(mongomock_db, fake_userinfo, monkeypatch):
    settings = _make_settings()
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GOOGLE] = FakeStrategy(OAuthProvider.GOOGLE, fake_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_state(mongomock_db, state_id="state-single-use")

    await complete_oauth(
        provider=OAuthProvider.GOOGLE,
        settings=settings,
        code="auth-code",
        state_id="state-single-use",
    )

    # State row should be gone after first use.
    assert await mongomock_db.oauth_states.find_one({"id": "state-single-use"}) is None

    # Second use should fail.
    with pytest.raises(ValueError, match="unknown or expired state"):
        await complete_oauth(
            provider=OAuthProvider.GOOGLE,
            settings=settings,
            code="auth-code",
            state_id="state-single-use",
        )


async def test_complete_oauth_provider_mismatch_raises(mongomock_db, fake_userinfo, monkeypatch):
    settings = _make_settings()
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GITHUB] = FakeStrategy(OAuthProvider.GITHUB, fake_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_state(
        mongomock_db,
        state_id="state-google-only",
        provider=OAuthProvider.GOOGLE,
    )

    with pytest.raises(ValueError, match="state/provider mismatch"):
        await complete_oauth(
            provider=OAuthProvider.GITHUB,
            settings=settings,
            code="auth-code",
            state_id="state-google-only",
        )


async def test_complete_oauth_expired_state_raises(mongomock_db, fake_userinfo, monkeypatch):
    settings = _make_settings()
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GOOGLE] = FakeStrategy(OAuthProvider.GOOGLE, fake_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_state(
        mongomock_db,
        state_id="state-expired",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="state expired"):
        await complete_oauth(
            provider=OAuthProvider.GOOGLE,
            settings=settings,
            code="auth-code",
            state_id="state-expired",
        )


async def test_complete_oauth_unknown_state_raises(mongomock_db):
    settings = _make_settings()
    register_default_strategies(settings)

    with pytest.raises(ValueError, match="unknown or expired state"):
        await complete_oauth(
            provider=OAuthProvider.GOOGLE,
            settings=settings,
            code="auth-code",
            state_id="state-not-real",
        )


# --- Userinfo parsing tests -----------------------------------------------------


async def test_google_strategy_parses_userinfo_response():
    """Google: ``sub``, ``email``, ``name``, ``picture``."""
    from glossa.oauth.google import GoogleStrategy

    settings = _make_settings()
    strategy = GoogleStrategy(settings=settings)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        return httpx.Response(
            200,
            json={
                "sub": "12345",
                "email": "bob@example.com",
                "name": "Bob",
                "picture": "https://example.com/b.png",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        info = await strategy.fetch_userinfo(client, "fake-token")

    assert info.provider_user_id == "12345"
    assert info.email == "bob@example.com"
    assert info.name == "Bob"
    assert info.avatar_url == "https://example.com/b.png"


async def test_github_strategy_falls_back_to_emails_endpoint_when_email_private():
    """GitHub keeps emails private by default — strategy fetches /user/emails."""
    from glossa.oauth.github import GithubStrategy

    settings = _make_settings()
    strategy = GithubStrategy(settings=settings)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={
                    "id": 4242,
                    "login": "carol",
                    "name": "Carol",
                    "email": None,
                    "avatar_url": "https://example.com/c.png",
                },
            )
        if request.url.path == "/user/emails":
            return httpx.Response(
                200,
                json=[
                    {"email": "other@example.com", "primary": False, "verified": True},
                    {"email": "carol@example.com", "primary": True, "verified": True},
                ],
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        info = await strategy.fetch_userinfo(client, "fake-token")

    assert info.provider_user_id == "4242"
    assert info.email == "carol@example.com"
    assert info.name == "Carol"
    assert info.avatar_url == "https://example.com/c.png"
    assert any("/user/emails" in c for c in calls)


# --- Registry tests -------------------------------------------------------------


def test_registry_resolves_known_providers():
    settings = _make_settings()
    register_default_strategies(settings)
    assert get_strategy(OAuthProvider.GOOGLE).provider == OAuthProvider.GOOGLE
    assert get_strategy(OAuthProvider.GITHUB).provider == OAuthProvider.GITHUB


def test_registry_raises_on_unknown_provider():
    settings = _make_settings()
    register_default_strategies(settings)
    # Empty registry first, then look up — but strict invariant: KeyError
    reset_registry()
    with pytest.raises(KeyError):
        get_strategy(OAuthProvider.GOOGLE)


# --- HTTP route tests (TestClient) ---------------------------------------------


def test_start_unknown_provider_404():
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get("/auth/myspace/start", follow_redirects=False)
    assert resp.status_code == 404


def test_start_redirects_to_provider_authorize_url():
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get("/auth/google/start", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=google-client-id" in location


def test_start_returns_503_when_provider_not_configured():
    settings = _make_settings(google_oauth_client_id=None)
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get("/auth/google/start", follow_redirects=False)
    assert resp.status_code == 503


def test_callback_missing_code_400():
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get("/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 400


def test_callback_unknown_provider_404():
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get("/auth/nope/callback?code=x&state=y", follow_redirects=False)
    assert resp.status_code == 404


async def test_callback_sets_session_cookie_and_redirects_to_dashboard(mongomock_db, fake_userinfo, monkeypatch):
    settings = _make_settings()
    from glossa.oauth import registry

    registry._REGISTRY[OAuthProvider.GOOGLE] = FakeStrategy(OAuthProvider.GOOGLE, fake_userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "access-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    await _seed_state(mongomock_db, state_id="state-route")

    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get(
        "/auth/google/callback?code=auth-code&state=state-route",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/"
    # Set-Cookie carries the session cookie.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "glossa_session=ses_" in set_cookie
    # The session row was created in DB.
    sessions = [s async for s in mongomock_db.sessions.find({})]
    assert len(sessions) == 1
    assert sessions[0]["id"].startswith("ses_")
    # And the user row exists.
    users = [u async for u in mongomock_db.users.find({})]
    assert len(users) == 1
    assert users[0]["email"] == "alice@example.com"


async def test_callback_returns_400_for_unknown_state(mongomock_db):
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.get(
        "/auth/google/callback?code=x&state=no-such-state",
        follow_redirects=False,
    )
    assert resp.status_code == 400


async def test_logout_destroys_session_and_clears_cookie(mongomock_db):
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)

    # Seed a user + session.
    now = datetime.now(UTC)
    user = User(
        id="usr_logout000001",
        email="x@example.com",
        name="X",
        oauth_accounts=[],
        created_at=now,
        last_login_at=now,
    )
    await mongomock_db.users.insert_one(user.model_dump())

    session = Session(
        id="ses_logout_target",
        user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
        ip=None,
        user_agent=None,
    )
    await mongomock_db.sessions.insert_one(session.model_dump())

    client = TestClient(app, cookies={"glossa_session": "ses_logout_target"})
    resp = client.post("/auth/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"
    # Cookie cleared (Max-Age=0).
    set_cookie = resp.headers.get("set-cookie", "")
    assert "glossa_session=" in set_cookie
    assert "Max-Age=0" in set_cookie
    # Session row gone.
    assert await mongomock_db.sessions.find_one({"id": "ses_logout_target"}) is None


def test_logout_without_cookie_still_redirects():
    settings = _make_settings()
    register_default_strategies(settings)
    app = _build_app(settings)
    client = TestClient(app)
    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"


# --- Misc check ----------------------------------------------------------------


def test_set_session_cookie_module_is_importable_from_route_module():
    """Sanity check that the auth route module's session-cookie helper is the
    same one D-Phase 2 exposes."""
    assert auth_routes.set_session_cookie is set_session_cookie
