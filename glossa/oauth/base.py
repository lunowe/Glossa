"""Strategy interface shared by Google + GitHub flows.

Each provider supplies its auth URL, token URL, userinfo URL, scope
string, and a parse_userinfo() that maps the provider's response shape
to a normalized OAuthUserInfo.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

from glossa.models.user import OAuthProvider


@dataclass(frozen=True)
class OAuthUserInfo:
    provider_user_id: str
    email: str
    name: str
    avatar_url: str | None = None


class OAuthProviderStrategy(Protocol):
    @property
    def provider(self) -> OAuthProvider: ...

    @property
    def authorize_url(self) -> str: ...

    @property
    def token_url(self) -> str: ...

    @property
    def userinfo_url(self) -> str: ...

    @property
    def scope(self) -> str: ...

    @property
    def client_id(self) -> str | None: ...

    @property
    def client_secret(self) -> str | None: ...

    async def fetch_userinfo(self, client: httpx.AsyncClient, access_token: str) -> OAuthUserInfo: ...
