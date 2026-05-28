"""Tests for the session layer: store + cookie helper + dependency.

The session layer is a separate identity surface from API keys: a dashboard
request carries ``Cookie: glossa_session=ses_...``, an API request carries
``Authorization: Bearer glsk_live_...``. These tests exercise the session
side only — see ``test_auth.py`` for the API side.
"""

from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.models.session import Session
from glossa.models.user import User
from glossa.sessions import (
    SessionContext,
    clear_session_cookie,
    create_session,
    destroy_session,
    get_session_user,
    require_session,
    set_session_cookie,
    touch_session,
)

# --- Helpers --------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    return Settings(**kwargs)


async def _seed_user(
    db,
    *,
    user_id: str = "usr_abc123",
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
    session_id: str = "ses_test_cookie_value",
    user_id: str = "usr_abc123",
    expires_at: datetime | None = None,
    last_seen_at: datetime | None = None,
) -> Session:
    now = datetime.now(UTC)
    session = Session(
        id=session_id,
        user_id=user_id,
        created_at=now,
        expires_at=expires_at or (now + timedelta(hours=24)),
        last_seen_at=last_seen_at or now,
        ip=None,
        user_agent=None,
    )
    await db.sessions.insert_one(session.model_dump())
    return session


def _build_app() -> FastAPI:
    """Build a minimal FastAPI app that exposes the session dependencies.

    We don't run the real lifespan — the mongomock fixture already wires up
    the DB client, and we set ``app.state.settings`` ourselves.
    """
    app = FastAPI()
    app.state.settings = _make_settings()

    @app.get("/whoami")
    async def whoami(ctx: SessionContext | None = Depends(get_session_user)):
        if ctx is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "email": ctx.user.email,
            "session_id": ctx.session_id,
        }

    @app.get("/strict")
    async def strict(ctx: SessionContext = Depends(require_session)):
        return {"email": ctx.user.email, "session_id": ctx.session_id}

    @app.post("/login-test")
    async def login_test(response: Response):
        set_session_cookie(
            response,
            session_id="ses_dummy_for_helper_test",
            settings=app.state.settings,
        )
        return {"ok": True}

    @app.post("/logout-test")
    async def logout_test(response: Response):
        clear_session_cookie(response, settings=app.state.settings)
        return {"ok": True}

    return app


# --- Store unit tests (no HTTP) -------------------------------------------------


async def test_create_session_inserts_row_with_ttl(mongomock_db):
    await _seed_user(mongomock_db, user_id="usr_store1")
    session = await create_session(user_id="usr_store1", ttl_hours=2)

    assert session.id.startswith("ses_")
    assert len(session.id) > len("ses_")  # has entropy after the prefix

    row = await mongomock_db.sessions.find_one({"id": session.id})
    assert row is not None
    assert row["user_id"] == "usr_store1"

    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(UTC)
    # Within 1 minute of the requested TTL
    delta = expires_at - datetime.now(UTC)
    assert timedelta(hours=2) - timedelta(minutes=1) <= delta <= timedelta(hours=2) + timedelta(minutes=1)


async def test_create_session_includes_ip_and_user_agent(mongomock_db):
    await _seed_user(mongomock_db, user_id="usr_store_meta")
    session = await create_session(
        user_id="usr_store_meta",
        ttl_hours=1,
        ip="203.0.113.4",
        user_agent="curl/8",
    )
    row = await mongomock_db.sessions.find_one({"id": session.id})
    assert row["ip"] == "203.0.113.4"
    assert row["user_agent"] == "curl/8"


async def test_destroy_session_removes_row(mongomock_db):
    await _seed_user(mongomock_db, user_id="usr_destroy")
    session = await create_session(user_id="usr_destroy", ttl_hours=1)

    await destroy_session(session.id)

    assert await mongomock_db.sessions.find_one({"id": session.id}) is None


async def test_destroy_session_missing_id_does_not_crash(mongomock_db):
    # Should not raise even when the row doesn't exist.
    await destroy_session("ses_does_not_exist")


async def test_touch_session_updates_last_seen_at(mongomock_db):
    await _seed_user(mongomock_db, user_id="usr_touch")
    old_last_seen = datetime.now(UTC) - timedelta(hours=1)
    await _seed_session(
        mongomock_db,
        session_id="ses_touch_target",
        user_id="usr_touch",
        last_seen_at=old_last_seen,
    )

    await touch_session("ses_touch_target")

    row = await mongomock_db.sessions.find_one({"id": "ses_touch_target"})
    new_last_seen = row["last_seen_at"]
    if new_last_seen.tzinfo is None:
        new_last_seen = new_last_seen.replace(tzinfo=UTC)
    assert new_last_seen > old_last_seen


async def test_touch_session_missing_id_does_not_crash(mongomock_db):
    # Best-effort — no error even when nothing matches.
    await touch_session("ses_no_such_session")


# --- Dependency tests (via TestClient) -----------------------------------------


def test_get_session_user_returns_none_without_cookie():
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


async def test_get_session_user_returns_context_with_valid_cookie(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_valid", email="valid@example.com")
    await _seed_session(
        mongomock_db,
        session_id="ses_valid_cookie",
        user_id="usr_valid",
    )

    client = TestClient(app, cookies={"glossa_session": "ses_valid_cookie"})
    resp = client.get("/whoami")

    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["email"] == "valid@example.com"
    assert body["session_id"] == "ses_valid_cookie"


async def test_get_session_user_returns_none_for_unknown_cookie(mongomock_db):
    app = _build_app()
    client = TestClient(app, cookies={"glossa_session": "ses_does_not_exist"})
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


async def test_get_session_user_returns_none_for_expired_session(mongomock_db):
    """A session whose ``expires_at`` is in the past must not authenticate,
    even if the TTL index hasn't pruned it yet (mongomock has no TTL).
    """
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_expired")
    await _seed_session(
        mongomock_db,
        session_id="ses_expired",
        user_id="usr_expired",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    client = TestClient(app, cookies={"glossa_session": "ses_expired"})
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


async def test_get_session_user_returns_none_for_unknown_user(mongomock_db):
    """Session row exists but its user_id points at a User that's been deleted."""
    app = _build_app()
    await _seed_session(
        mongomock_db,
        session_id="ses_orphan",
        user_id="usr_does_not_exist",
    )

    client = TestClient(app, cookies={"glossa_session": "ses_orphan"})
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_require_session_returns_303_for_html_accept():
    app = _build_app()
    client = TestClient(app)
    resp = client.get(
        "/strict",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"


def test_require_session_returns_401_for_htmx_request():
    app = _build_app()
    client = TestClient(app)
    resp = client.get(
        "/strict",
        headers={"HX-Request": "true", "Accept": "text/html"},
    )
    assert resp.status_code == 401
    assert resp.headers["hx-redirect"] == "/dashboard/login"


def test_require_session_returns_401_for_json_request():
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/strict")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "login required"


async def test_require_session_returns_context_with_valid_cookie(mongomock_db):
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_strict", email="strict@example.com")
    await _seed_session(
        mongomock_db,
        session_id="ses_strict_cookie",
        user_id="usr_strict",
    )

    client = TestClient(app, cookies={"glossa_session": "ses_strict_cookie"})
    resp = client.get("/strict")

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "strict@example.com"
    assert body["session_id"] == "ses_strict_cookie"


async def test_dependency_bumps_last_seen_at(mongomock_db):
    """The dependency should touch ``last_seen_at`` on each successful resolve."""
    app = _build_app()
    await _seed_user(mongomock_db, user_id="usr_lastseen")
    old_last_seen = datetime.now(UTC) - timedelta(hours=2)
    await _seed_session(
        mongomock_db,
        session_id="ses_lastseen",
        user_id="usr_lastseen",
        last_seen_at=old_last_seen,
    )

    client = TestClient(app, cookies={"glossa_session": "ses_lastseen"})
    resp = client.get("/whoami")
    assert resp.status_code == 200

    row = await mongomock_db.sessions.find_one({"id": "ses_lastseen"})
    new_last_seen = row["last_seen_at"]
    if new_last_seen.tzinfo is None:
        new_last_seen = new_last_seen.replace(tzinfo=UTC)
    assert new_last_seen > old_last_seen


# --- Cookie helper tests --------------------------------------------------------


def _parse_cookie_attrs(set_cookie: str) -> dict[str, str | bool]:
    """Parse a Set-Cookie header into {attr_lower: value or True}."""
    parts = [p.strip() for p in set_cookie.split(";")]
    attrs: dict[str, str | bool] = {}
    # First part is name=value
    name, _, value = parts[0].partition("=")
    attrs["__name__"] = name
    attrs["__value__"] = value
    for part in parts[1:]:
        if "=" in part:
            k, _, v = part.partition("=")
            attrs[k.lower()] = v
        else:
            attrs[part.lower()] = True
    return attrs


def test_set_session_cookie_sets_httponly_lax_path():
    app = _build_app()
    client = TestClient(app)
    resp = client.post("/login-test")
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie")
    assert set_cookie is not None
    attrs = _parse_cookie_attrs(set_cookie)

    assert attrs["__name__"] == "glossa_session"
    assert attrs["__value__"] == "ses_dummy_for_helper_test"
    assert attrs.get("httponly") is True
    assert attrs.get("path") == "/"
    samesite = attrs.get("samesite")
    assert isinstance(samesite, str) and samesite.lower() == "lax"
    # Secure flag is off by default in tests.
    assert attrs.get("secure") is not True


def test_set_session_cookie_max_age_matches_ttl():
    app = _build_app()
    # Settings default: 168 hours.
    expected_max_age = app.state.settings.session_ttl_hours * 3600

    client = TestClient(app)
    resp = client.post("/login-test")
    set_cookie = resp.headers.get("set-cookie")
    attrs = _parse_cookie_attrs(set_cookie)
    assert int(attrs["max-age"]) == expected_max_age


def test_set_session_cookie_honors_secure_flag():
    """When ``session_cookie_secure=True`` the Secure attribute is set."""
    app = FastAPI()
    app.state.settings = _make_settings(session_cookie_secure=True)

    @app.post("/login")
    async def login(response: Response):
        set_session_cookie(
            response,
            session_id="ses_secure_value",
            settings=app.state.settings,
        )
        return {"ok": True}

    client = TestClient(app)
    resp = client.post("/login")
    set_cookie = resp.headers.get("set-cookie")
    attrs = _parse_cookie_attrs(set_cookie)
    assert attrs.get("secure") is True


def test_clear_session_cookie_deletes_cookie():
    app = _build_app()
    client = TestClient(app)
    resp = client.post("/logout-test")
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie")
    assert set_cookie is not None
    attrs = _parse_cookie_attrs(set_cookie)
    assert attrs["__name__"] == "glossa_session"
    # Starlette's delete_cookie sets Max-Age=0 and an expiry in the past.
    assert attrs.get("max-age") == "0"
    assert attrs.get("path") == "/"
