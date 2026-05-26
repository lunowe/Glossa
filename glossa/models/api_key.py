from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Scope(StrEnum):
    SPACES_READ = "spaces:read"
    SPACES_WRITE = "spaces:write"
    SOURCES_WRITE = "sources:write"
    QUERY = "query"
    LINT = "lint"
    ADMIN = "admin"


DEFAULT_SCOPES: list[Scope] = [
    Scope.SPACES_READ,
    Scope.SPACES_WRITE,
    Scope.SOURCES_WRITE,
    Scope.QUERY,
    Scope.LINT,
]


class ApiKey(BaseModel):
    id: str  # key_<12 hex>
    tenant_id: str
    hashed_key: str
    prefix: str
    label: str | None = None
    scopes: list[Scope] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    label: str | None = None
    scopes: list[Scope] | None = None


class ApiKeyIssued(BaseModel):
    """Returned ONCE at creation, with the plaintext key. The plaintext is never stored."""

    api_key: ApiKey
    plaintext: str


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, hashed_key)."""
    import hashlib
    import secrets

    random_part = secrets.token_urlsafe(32)
    plaintext = f"glsk_live_{random_part}"
    prefix = f"glsk_live_{random_part[:8]}"
    hashed = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, prefix, hashed


def hash_key(plaintext: str) -> str:
    import hashlib

    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
