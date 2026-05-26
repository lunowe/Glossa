"""Shared helpers for tenant-scoping at the route layer."""

from glossa.auth.context import AuthContext
from glossa.models.api_key import Scope


def is_admin(ctx: AuthContext) -> bool:
    """System contexts and keys with the admin scope bypass tenant filters."""
    return ctx.is_system or ctx.has_scope(Scope.ADMIN)


def space_query(space_id: str, ctx: AuthContext) -> dict:
    """Mongo filter for a space lookup; tenant-filtered unless caller is admin."""
    q: dict = {"id": space_id}
    if not is_admin(ctx):
        q["tenant_id"] = ctx.tenant_id
    return q


def tenant_scope_filter(ctx: AuthContext, requested_tenant_id: str | None = None) -> str:
    """Resolve the tenant_id a list/usage endpoint should operate against.

    Non-admins always see their own tenant. Admins can pass an explicit
    tenant_id; if they don't, they see their own.
    """
    if is_admin(ctx) and requested_tenant_id:
        return requested_tenant_id
    return ctx.tenant_id
