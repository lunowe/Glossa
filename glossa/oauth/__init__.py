from glossa.oauth.base import OAuthProviderStrategy, OAuthUserInfo
from glossa.oauth.flow import begin_oauth, complete_oauth
from glossa.oauth.github import GithubStrategy
from glossa.oauth.google import GoogleStrategy
from glossa.oauth.registry import get_strategy, register_default_strategies

__all__ = [
    "GithubStrategy",
    "GoogleStrategy",
    "OAuthProviderStrategy",
    "OAuthUserInfo",
    "begin_oauth",
    "complete_oauth",
    "get_strategy",
    "register_default_strategies",
]
