"""Session lifecycle: create, destroy, touch (refresh last_seen_at)."""

import contextlib
import secrets
from datetime import UTC, datetime, timedelta

from glossa.db.client import get_db
from glossa.models.session import Session


def _now() -> datetime:
    return datetime.now(UTC)


async def create_session(
    *,
    user_id: str,
    ttl_hours: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Session:
    now = _now()
    session = Session(
        id=f"ses_{secrets.token_urlsafe(32)}",
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
        last_seen_at=now,
        ip=ip,
        user_agent=user_agent,
    )
    db = get_db()
    await db.sessions.insert_one(session.model_dump())
    return session


async def destroy_session(session_id: str) -> None:
    db = get_db()
    await db.sessions.delete_one({"id": session_id})


async def touch_session(session_id: str) -> None:
    """Bump last_seen_at. Best-effort — failures don't propagate."""
    db = get_db()
    with contextlib.suppress(Exception):
        await db.sessions.update_one(
            {"id": session_id},
            {"$set": {"last_seen_at": _now()}},
        )
