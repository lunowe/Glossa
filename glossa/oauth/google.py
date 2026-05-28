"""Google OAuth 2.0 with PKCE. Uses OIDC userinfo endpoint."""

from dataclasses import dataclass

import httpx

from glossa.config import Settings
from glossa.models.user import OAuthProvider
from glossa.oauth.base import OAuthUserInfo


@dataclass(frozen=True)
class GoogleStrategy:
    settings: Settings

    @property
    def provider(self) -> OAuthProvider:
        return OAuthProvider.GOOGLE

    @property
    def authorize_url(self) -> str:
        return "https://accounts.google.com/o/oauth2/v2/auth"

    @property
    def token_url(self) -> str:
        return "https://oauth2.googleapis.com/token"

    @property
    def userinfo_url(self) -> str:
        return "https://openidconnect.googleapis.com/v1/userinfo"

    @property
    def scope(self) -> str:
        return "openid email profile"

    @property
    def client_id(self) -> str | None:
        return self.settings.google_oauth_client_id

    @property
    def client_secret(self) -> str | None:
        return self.settings.google_oauth_client_secret

    async def fetch_userinfo(self, client: httpx.AsyncClient, access_token: str) -> OAuthUserInfo:
        resp = await client.get(
            self.userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return OAuthUserInfo(
            provider_user_id=str(data["sub"]),
            email=data["email"],
            name=data.get("name") or data["email"].split("@")[0],
            avatar_url=data.get("picture"),
        )
