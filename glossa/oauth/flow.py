"""OAuth code-flow orchestration. Provider-agnostic; uses strategy registry."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from glossa.config import Settings
from glossa.db.client import get_db
from glossa.models.membership import TenantMember, TenantRole
from glossa.models.oauth_state import OAuthState
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.models.user import OAuthAccount, OAuthProvider, User
from glossa.oauth.base import OAuthProviderStrategy, OAuthUserInfo
from glossa.oauth.registry import get_strategy


@dataclass(frozen=True)
class BeginResult:
    authorize_url: str
    state_id: str


@dataclass(frozen=True)
class CompleteResult:
    user: User
    is_new_user: bool
    redirect_to: str | None = None


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge). S256 method."""
    verifier = secrets.token_urlsafe(64)[:64]  # 43-128 chars per RFC
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def begin_oauth(
    *,
    provider: OAuthProvider,
    settings: Settings,
    redirect_to: str | None = None,
) -> BeginResult:
    strategy = get_strategy(provider)
    if not strategy.client_id:
        raise ValueError(f"{provider.value} oauth client_id not configured")

    code_verifier, code_challenge = _pkce_pair()
    state_id = secrets.token_urlsafe(32)
    now = datetime.now(UTC)

    db = get_db()
    state = OAuthState(
        id=state_id,
        provider=provider,
        code_verifier=code_verifier,
        redirect_to=redirect_to,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.oauth_state_ttl_minutes),
    )
    await db.oauth_states.insert_one(state.model_dump())

    redirect_uri = f"{settings.base_url.rstrip('/')}/auth/{provider.value}/callback"
    params = {
        "response_type": "code",
        "client_id": strategy.client_id,
        "redirect_uri": redirect_uri,
        "scope": strategy.scope,
        "state": state_id,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",  # Google: don't ask for refresh token
        "prompt": "select_account",  # Google: let user pick account
    }
    return BeginResult(
        authorize_url=f"{strategy.authorize_url}?{urlencode(params)}",
        state_id=state_id,
    )


async def _exchange_code(
    strategy: OAuthProviderStrategy,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client: httpx.AsyncClient,
) -> str:
    """POST the authorization code; return access_token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": strategy.client_id,
        "client_secret": strategy.client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {"Accept": "application/json"}
    resp = await client.post(strategy.token_url, data=data, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise httpx.HTTPError(f"no access_token in token response: {payload!r}")
    return token


async def _upsert_user(userinfo: OAuthUserInfo, provider: OAuthProvider) -> tuple[User, bool]:
    """Upsert by email. Append OAuthAccount if this provider isn't linked yet.

    Returns (user, is_new_user).
    """
    db = get_db()
    now = datetime.now(UTC)
    existing = await db.users.find_one({"email": userinfo.email})
    if existing:
        user = User.model_validate(existing)
        # Append the account if not already linked
        already_linked = any(
            acct.provider == provider and acct.provider_user_id == userinfo.provider_user_id
            for acct in user.oauth_accounts
        )
        updates: dict = {"last_login_at": now}
        if not already_linked:
            new_acct = OAuthAccount(
                provider=provider,
                provider_user_id=userinfo.provider_user_id,
                email=userinfo.email,
                linked_at=now,
            )
            user_accounts = [*user.oauth_accounts, new_acct]
            updates["oauth_accounts"] = [a.model_dump() for a in user_accounts]
        # Refresh display fields if the provider has them
        if userinfo.name and userinfo.name != user.name:
            updates["name"] = userinfo.name
        if userinfo.avatar_url and userinfo.avatar_url != user.avatar_url:
            updates["avatar_url"] = userinfo.avatar_url
        await db.users.update_one({"id": user.id}, {"$set": updates})
        refreshed = await db.users.find_one({"id": user.id})
        return User.model_validate(refreshed), False

    user = User(
        id=f"usr_{uuid4().hex[:12]}",
        email=userinfo.email,
        name=userinfo.name,
        avatar_url=userinfo.avatar_url,
        oauth_accounts=[
            OAuthAccount(
                provider=provider,
                provider_user_id=userinfo.provider_user_id,
                email=userinfo.email,
                linked_at=now,
            )
        ],
        created_at=now,
        last_login_at=now,
    )
    await db.users.insert_one(user.model_dump())
    return user, True


async def _ensure_initial_tenant(user: User) -> None:
    """If a brand-new user has no tenant memberships, auto-create one."""
    db = get_db()
    has_membership = await db.tenant_members.find_one({"user_id": user.id}, {"id": 1})
    if has_membership:
        return
    now = datetime.now(UTC)
    tenant = Tenant(
        id=f"tnt_{uuid4().hex[:12]}",
        name=f"{user.name}'s Workspace",
        owner_email=user.email,
        plan=TenantPlan.FREE,
        status=TenantStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    await db.tenants.insert_one(tenant.model_dump())
    member = TenantMember(
        id=f"mem_{uuid4().hex[:12]}",
        tenant_id=tenant.id,
        user_id=user.id,
        role=TenantRole.OWNER,
        joined_at=now,
    )
    await db.tenant_members.insert_one(member.model_dump())


async def complete_oauth(
    *,
    provider: OAuthProvider,
    settings: Settings,
    code: str,
    state_id: str,
    client: httpx.AsyncClient | None = None,
) -> CompleteResult:
    db = get_db()
    state_doc = await db.oauth_states.find_one({"id": state_id})
    if not state_doc:
        raise ValueError("unknown or expired state")
    state = OAuthState.model_validate(state_doc)
    if state.provider != provider:
        raise ValueError("state/provider mismatch")
    expires_at = state.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise ValueError("state expired")

    # Capture before delete; state is single-use
    redirect_to = state.redirect_to

    # State is single-use
    await db.oauth_states.delete_one({"id": state_id})

    strategy = get_strategy(provider)
    redirect_uri = f"{settings.base_url.rstrip('/')}/auth/{provider.value}/callback"

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        access_token = await _exchange_code(
            strategy,
            code=code,
            code_verifier=state.code_verifier,
            redirect_uri=redirect_uri,
            client=client,
        )
        userinfo = await strategy.fetch_userinfo(client, access_token)
    finally:
        if owns_client:
            await client.aclose()

    user, is_new = await _upsert_user(userinfo, provider)
    if is_new:
        await _ensure_initial_tenant(user)

    return CompleteResult(user=user, is_new_user=is_new, redirect_to=redirect_to)
