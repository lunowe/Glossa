"""Authentication context attached to every authenticated request."""

from dataclasses import dataclass

from glossa.models.api_key import Scope


@dataclass(frozen=True)
class AuthContext:
    """Resolved identity for one request.

    Either a real API key (``api_key_id`` set, ``is_system=False``) or a
    synthetic system context (``api_key_id=None``, ``is_system=True``) used
    for self-host mode and the bootstrap admin escape hatch. System contexts
    have all scopes implicitly.
    """

    tenant_id: str
    api_key_id: str | None
    scopes: tuple[Scope, ...]
    is_system: bool = False

    def has_scope(self, scope: Scope) -> bool:
        return self.is_system or scope in self.scopes

    @classmethod
    def system(cls, *, tenant_id: str = "_system") -> "AuthContext":
        return cls(
            tenant_id=tenant_id,
            api_key_id=None,
            scopes=tuple(Scope),
            is_system=True,
        )
