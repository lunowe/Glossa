"""Dashboard identity attached to an authenticated session request."""

from dataclasses import dataclass

from glossa.models.user import User


@dataclass(frozen=True)
class SessionContext:
    """Resolved dashboard identity for one request.

    Holds the User + the Session id (so routes can act on the current
    session — e.g. logout invalidates *this* session, not all of them).
    """

    user: User
    session_id: str
