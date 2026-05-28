"""Tests for User, TenantMember, Invite, Session, and OAuthState models."""

from datetime import UTC, datetime, timedelta

from glossa.models.membership import (
    Invite,
    TenantMember,
    TenantRole,
)
from glossa.models.oauth_state import OAuthState
from glossa.models.session import Session
from glossa.models.user import OAuthAccount, OAuthProvider, User


def test_user_pydantic_roundtrip():
    now = datetime.now(UTC)
    user = User(
        id="usr_abc123def456",
        email="alice@example.com",
        name="Alice",
        avatar_url="https://example.com/a.png",
        oauth_accounts=[
            OAuthAccount(
                provider=OAuthProvider.GOOGLE,
                provider_user_id="google-sub-123",
                email="alice@example.com",
                linked_at=now,
            )
        ],
        created_at=now,
        last_login_at=now,
    )
    dumped = user.model_dump()
    restored = User.model_validate(dumped)
    assert restored.id == user.id
    assert restored.email == user.email
    assert restored.name == user.name
    assert restored.avatar_url == user.avatar_url
    assert restored.created_at == user.created_at
    assert restored.last_login_at == user.last_login_at
    assert len(restored.oauth_accounts) == 1
    account = restored.oauth_accounts[0]
    assert account.provider == OAuthProvider.GOOGLE
    assert account.provider_user_id == "google-sub-123"
    assert account.email == "alice@example.com"
    assert account.linked_at == now


def test_user_default_oauth_accounts_empty():
    now = datetime.now(UTC)
    user = User(
        id="usr_abc123def456",
        email="bob@example.com",
        name="Bob",
        created_at=now,
    )
    assert user.oauth_accounts == []
    assert user.avatar_url is None
    assert user.last_login_at is None


def test_tenant_member_pydantic_roundtrip():
    now = datetime.now(UTC)
    member = TenantMember(
        id="mem_abc123def456",
        tenant_id="tnt_xxx",
        user_id="usr_yyy",
        role=TenantRole.ADMIN,
        joined_at=now,
    )
    dumped = member.model_dump()
    restored = TenantMember.model_validate(dumped)
    assert restored.id == member.id
    assert restored.tenant_id == member.tenant_id
    assert restored.user_id == member.user_id
    assert restored.role == TenantRole.ADMIN
    assert restored.joined_at == member.joined_at


def test_invite_pydantic_roundtrip():
    now = datetime.now(UTC)
    expires = now + timedelta(hours=168)
    invite = Invite(
        id="inv_abc123def456",
        tenant_id="tnt_xxx",
        token="some-url-safe-token",
        role=TenantRole.MEMBER,
        created_by_user_id="usr_yyy",
        created_at=now,
        expires_at=expires,
    )
    dumped = invite.model_dump()
    restored = Invite.model_validate(dumped)
    assert restored.id == invite.id
    assert restored.tenant_id == invite.tenant_id
    assert restored.token == invite.token
    assert restored.role == TenantRole.MEMBER
    assert restored.created_by_user_id == invite.created_by_user_id
    assert restored.created_at == invite.created_at
    assert restored.expires_at == invite.expires_at
    assert restored.accepted_at is None
    assert restored.revoked_at is None


def test_session_pydantic_roundtrip():
    now = datetime.now(UTC)
    expires = now + timedelta(days=30)
    session = Session(
        id="ses_some_urlsafe_value",
        user_id="usr_yyy",
        created_at=now,
        expires_at=expires,
        last_seen_at=now,
        ip="127.0.0.1",
        user_agent="Mozilla/5.0",
    )
    dumped = session.model_dump()
    restored = Session.model_validate(dumped)
    assert restored.id == session.id
    assert restored.user_id == session.user_id
    assert restored.created_at == session.created_at
    assert restored.expires_at == session.expires_at
    assert restored.last_seen_at == session.last_seen_at
    assert restored.ip == "127.0.0.1"
    assert restored.user_agent == "Mozilla/5.0"


def test_oauth_state_pydantic_roundtrip():
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=10)
    state = OAuthState(
        id="some_urlsafe_state",
        provider=OAuthProvider.GITHUB,
        code_verifier="pkce-verifier-value",
        redirect_to="/dashboard",
        created_at=now,
        expires_at=expires,
    )
    dumped = state.model_dump()
    restored = OAuthState.model_validate(dumped)
    assert restored.id == state.id
    assert restored.provider == OAuthProvider.GITHUB
    assert restored.code_verifier == state.code_verifier
    assert restored.redirect_to == "/dashboard"
    assert restored.created_at == state.created_at
    assert restored.expires_at == state.expires_at


def test_tenant_role_enum_values():
    assert TenantRole.OWNER == "owner"
    assert TenantRole.ADMIN == "admin"
    assert TenantRole.MEMBER == "member"


def test_oauth_provider_enum_values():
    assert OAuthProvider.GOOGLE == "google"
    assert OAuthProvider.GITHUB == "github"
