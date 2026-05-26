"""FastAPI dependency that resolves Bearer tokens into AuthContext."""

import logging
from datetime import UTC, datetime
from hmac import compare_digest

from fastapi import Depends, HTTPException, Request

from glossa.auth.context import AuthContext
from glossa.db.client import get_db
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.tenant import Tenant, TenantStatus

logger = logging.getLogger(__name__)


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def get_auth_context(request: Request) -> AuthContext:
    settings = request.app.state.settings
    token = _extract_bearer(request.headers.get("Authorization"))

    if token is None:
        if not settings.auth_required:
            ctx = AuthContext.system()
            request.state.auth = ctx
            return ctx
        raise HTTPException(status_code=401, detail="missing Authorization header")

    bootstrap = settings.bootstrap_admin_api_key
    if bootstrap and compare_digest(token, bootstrap):
        ctx = AuthContext.system()
        request.state.auth = ctx
        return ctx

    db = get_db()
    key_doc = await db.api_keys.find_one({"hashed_key": hash_key(token)})
    if not key_doc:
        raise HTTPException(status_code=401, detail="invalid api key")
    api_key = ApiKey.model_validate(key_doc)
    if api_key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="invalid api key")

    tenant_doc = await db.tenants.find_one({"id": api_key.tenant_id})
    if not tenant_doc:
        raise HTTPException(status_code=401, detail="invalid api key")
    tenant = Tenant.model_validate(tenant_doc)
    if tenant.status != TenantStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="tenant suspended")

    try:
        await db.api_keys.update_one(
            {"id": api_key.id},
            {"$set": {"last_used_at": datetime.now(UTC)}},
        )
    except Exception:
        logger.warning("failed to update last_used_at for api_key=%s", api_key.id, exc_info=True)

    ctx = AuthContext(
        tenant_id=tenant.id,
        api_key_id=api_key.id,
        scopes=tuple(api_key.scopes),
        is_system=False,
    )
    request.state.auth = ctx
    return ctx


def require_scope(scope: Scope):
    async def _checker(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"missing scope: {scope.value}")
        return ctx

    return _checker
