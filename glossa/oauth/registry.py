"""Strategy registry — resolves provider name → strategy instance."""

from glossa.config import Settings
from glossa.models.user import OAuthProvider
from glossa.oauth.base import OAuthProviderStrategy
from glossa.oauth.github import GithubStrategy
from glossa.oauth.google import GoogleStrategy

_REGISTRY: dict[OAuthProvider, OAuthProviderStrategy] = {}


def register_default_strategies(settings: Settings) -> None:
    """Populate registry once at app startup. Idempotent."""
    _REGISTRY[OAuthProvider.GOOGLE] = GoogleStrategy(settings=settings)
    _REGISTRY[OAuthProvider.GITHUB] = GithubStrategy(settings=settings)


def get_strategy(provider: OAuthProvider) -> OAuthProviderStrategy:
    if provider not in _REGISTRY:
        raise KeyError(f"unknown provider: {provider}")
    return _REGISTRY[provider]


def reset_registry() -> None:
    """Test-only: clear the registry between fixtures."""
    _REGISTRY.clear()
