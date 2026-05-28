"""FastAPI dependency: resolve the session cookie to a SessionContext."""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, Request

from glossa.db.client import get_db
from glossa.models.session import Session
from glossa.models.user import User
from glossa.sessions.context import SessionContext
from glossa.sessions.store import touch_session

logger = logging.getLogger(__name__)


async def _resolve(request: Request) -> SessionContext | None:
    """Return SessionContext if cookie is valid, None if absent / invalid / expired."""
    settings = request.app.state.settings
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        return None

    db = get_db()
    session_doc = await db.sessions.find_one({"id": cookie})
    if not session_doc:
        return None
    session = Session.model_validate(session_doc)

    # Defensive expiry check (TTL index handles eventual cleanup, but it's
    # not guaranteed to be instant).
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        return None

    user_doc = await db.users.find_one({"id": session.user_id})
    if not user_doc:
        return None
    user = User.model_validate(user_doc)

    # Best-effort touch — don't fail the request if the write fails.
    try:
        await touch_session(session.id)
    except Exception:
        logger.warning("failed to touch session=%s", session.id, exc_info=True)

    return SessionContext(user=user, session_id=session.id)


async def get_session_user(request: Request) -> SessionContext | None:
    """Optional session resolution. Routes that allow anonymous (login page,
    invite-accept landing) use this directly and branch on None.
    """
    return await _resolve(request)


async def require_session(request: Request) -> SessionContext:
    """Strict session — raises 401 (API-style) or returns a Redirect (browser).

    For dashboard pages we want a real HTTP redirect to /dashboard/login.
    Detect HTML accept and behave accordingly; JSON callers (HTMX fragment
    requests) get 401 with a header that HTMX understands.
    """
    ctx = await _resolve(request)
    if ctx is not None:
        return ctx

    accept = request.headers.get("accept", "")
    is_htmx = request.headers.get("hx-request") == "true"

    if is_htmx:
        # HTMX: trigger a client-side redirect via the response header.
        raise HTTPException(
            status_code=401,
            detail="login required",
            headers={"HX-Redirect": "/dashboard/login"},
        )
    if "text/html" in accept:
        # Full-page browser request: server-side redirect.
        raise HTTPException(
            status_code=303,
            detail="login required",
            headers={"Location": "/dashboard/login"},
        )
    raise HTTPException(status_code=401, detail="login required")
