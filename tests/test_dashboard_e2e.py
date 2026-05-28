"""End-to-end integration test for the wired Glossa app.

This is the test that proves "all the pieces really compose into a hosted
product." It drives the actual ``glossa.main.app`` (the real wired
application — same router set, same middleware, same dependency graph)
through the full new-user lifecycle:

  1. OAuth callback (Google) → user + workspace + session cookie created
  2. /dashboard/ lists the workspace
  3. /dashboard/t/{tid}/keys issues a key (plaintext returned once via PRG)
  4. POST /spaces with that key creates a space scoped to the tenant
  5. /dashboard/t/{tid}/activity surfaces the POST in the audit feed

Approach:
- Use ``glossa.main.app`` directly so the wiring is exercised end-to-end.
- Wire ``app.state`` ourselves (mirroring the lifespan handler) so we
  don't need a live MinIO/Mongo; the autouse ``mongomock_db`` fixture
  already swapped the global DB client to mongomock-motor and we install
  an in-memory storage backend.
- Inject a ``FakeStrategy`` into the OAuth registry and monkeypatch
  ``_exchange_code`` so no real HTTP is performed during the callback.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.main import app
from glossa.models.oauth_state import OAuthState
from glossa.models.user import OAuthProvider
from glossa.oauth import flow as flow_mod
from glossa.oauth import registry as oauth_registry
from glossa.oauth.base import OAuthUserInfo
from glossa.oauth.registry import register_default_strategies, reset_registry
from glossa.storage.memory import InMemoryStorageBackend


class _FakeStrategy:
    """Stand-in OAuth strategy that returns canned userinfo with no HTTP."""

    def __init__(self, provider: OAuthProvider, userinfo: OAuthUserInfo) -> None:
        self.provider = provider
        self.client_id = "test-client"
        self.client_secret = "test-secret"
        self.authorize_url = "https://fake-provider.test/authorize"
        self.token_url = "https://fake-provider.test/token"
        self.userinfo_url = "https://fake-provider.test/userinfo"
        self.scope = "openid email"
        self._userinfo = userinfo

    async def fetch_userinfo(self, client, access_token):
        return self._userinfo


@pytest.fixture(autouse=True)
def _wire_app_state_and_reset_registry():
    """Mirror the lifespan handler for tests.

    The real lifespan would call ``init_db`` (replaced by mongomock fixture),
    set ``settings`` + ``storage`` on app.state, and register OAuth
    strategies. We do those by hand and reset the OAuth registry on each
    side of the fixture so state doesn't leak between tests.
    """
    reset_registry()
    yield
    reset_registry()


def _settings(*, auth_required: bool = False) -> Settings:
    return Settings(
        auth_required=auth_required,
        google_oauth_client_id="google-client-id",
        google_oauth_client_secret="google-client-secret",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret",
        base_url="http://test.example",
    )


async def _seed_oauth_state(db, *, state_id: str, provider: OAuthProvider) -> OAuthState:
    now = datetime.now(UTC)
    state = OAuthState(
        id=state_id,
        provider=provider,
        code_verifier="verifier-fixed",
        redirect_to=None,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    await db.oauth_states.insert_one(state.model_dump())
    return state


_TENANT_LINK_RE = re.compile(r'href="/dashboard/t/(tnt_[a-f0-9]{12})/"')


async def test_full_signup_to_first_space_via_dashboard(mongomock_db, monkeypatch):
    """Full sign-up → tenant-created → key-issued → API-call → activity loop.

    The bearer-token API path requires ``auth_required=True`` so the auth
    dependency actually validates the issued key against the DB instead of
    falling back to system mode.
    """
    settings = _settings(auth_required=True)
    app.state.settings = settings
    app.state.storage = InMemoryStorageBackend()
    register_default_strategies(settings)

    # Inject a fake OAuth strategy + stub the token exchange so the
    # callback does not perform any real HTTP.
    userinfo = OAuthUserInfo(
        provider_user_id="oauth-google-1",
        email="ada@example.com",
        name="Ada Lovelace",
        avatar_url=None,
    )
    oauth_registry._REGISTRY[OAuthProvider.GOOGLE] = _FakeStrategy(OAuthProvider.GOOGLE, userinfo)

    async def _fake_exchange(*args, **kwargs):
        return "fake-token"

    monkeypatch.setattr(flow_mod, "_exchange_code", _fake_exchange)

    # Pre-seed the OAuth state row that the callback expects.
    await _seed_oauth_state(mongomock_db, state_id="state-e2e", provider=OAuthProvider.GOOGLE)

    client = TestClient(app, follow_redirects=False)

    # --- 1. OAuth callback creates the user + workspace + session ----------
    resp = client.get("/auth/google/callback?code=fake&state=state-e2e")
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/dashboard/"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "glossa_session=ses_" in set_cookie, set_cookie

    # The TestClient remembers Set-Cookie automatically across calls.
    session_cookie = client.cookies.get("glossa_session")
    assert session_cookie is not None and session_cookie.startswith("ses_")

    # The user + tenant rows exist in the DB.
    users = [u async for u in mongomock_db.users.find({})]
    assert len(users) == 1
    assert users[0]["email"] == "ada@example.com"
    tenants = [t async for t in mongomock_db.tenants.find({})]
    assert len(tenants) == 1
    auto_tenant = tenants[0]
    assert "Ada" in auto_tenant["name"]
    members = [m async for m in mongomock_db.tenant_members.find({})]
    assert len(members) == 1
    assert members[0]["role"] == "owner"

    # --- 2. Dashboard index lists the auto-created workspace ---------------
    # /dashboard/ → 307 redirect → /dashboard (FastAPI trailing-slash behavior),
    # so follow that one redirect to land on the index.
    resp = client.get("/dashboard/", follow_redirects=True)
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Jinja autoescapes the apostrophe in "Ada Lovelace's Workspace", so
    # match the unambiguous parts directly.
    assert "Ada Lovelace" in body
    assert "Workspace" in body
    match = _TENANT_LINK_RE.search(body)
    assert match is not None, f"no tenant link found in dashboard index: {body[:500]}"
    tenant_id = match.group(1)
    assert tenant_id == auto_tenant["id"]

    # --- 3. Issue a key from the dashboard ---------------------------------
    resp = client.post(
        f"/dashboard/t/{tenant_id}/keys",
        data={
            "label": "e2e",
            "scopes": ["spaces:read", "spaces:write", "sources:write"],
        },
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith(f"/dashboard/t/{tenant_id}/keys?"), location
    qs = parse_qs(urlparse(location).query)
    assert "new_plaintext" in qs, location
    plaintext = qs["new_plaintext"][0]
    assert plaintext.startswith("glsk_live_"), plaintext

    # Persisted hash + label + scopes
    key_docs = [doc async for doc in mongomock_db.api_keys.find({"tenant_id": tenant_id})]
    assert len(key_docs) == 1
    assert key_docs[0]["label"] == "e2e"
    assert set(key_docs[0]["scopes"]) == {"spaces:read", "spaces:write", "sources:write"}

    # --- 4. Use the issued key against the API to create a space ----------
    api_client = TestClient(app, follow_redirects=False)
    resp = api_client.post(
        "/spaces",
        json={"name": "Project Apollo"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200, resp.text
    space = resp.json()
    assert space["tenant_id"] == tenant_id
    assert space["name"] == "Project Apollo"
    assert space["id"].startswith("gls_")

    # --- 5. The /spaces POST shows up in the activity feed ----------------
    # The ActivityMiddleware records every non-/healthz request.
    activity_resp = client.get(f"/dashboard/t/{tenant_id}/activity")
    assert activity_resp.status_code == 200, activity_resp.text
    activity_body = activity_resp.text
    # The activity table renders POST / GET cells as <code>METHOD</code>.
    assert "<code>POST</code>" in activity_body
    assert "<code>/spaces</code>" in activity_body


def test_healthz_is_public_with_auth_required(mongomock_db):
    """Even with ``auth_required=True``, /healthz must remain anonymous.

    /healthz is the liveness probe — putting it behind the bearer wall
    would prevent load balancers from ever marking the app healthy. The
    route function takes no auth dependency, so this should hold.
    """
    settings = _settings(auth_required=True)
    app.state.settings = settings
    app.state.storage = InMemoryStorageBackend()
    register_default_strategies(settings)

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ok"
    assert "version" in payload
