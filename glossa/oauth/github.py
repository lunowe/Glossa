"""GitHub OAuth 2.0 with PKCE. Email may be private — falls back to /user/emails."""

from dataclasses import dataclass

import httpx

from glossa.config import Settings
from glossa.models.user import OAuthProvider
from glossa.oauth.base import OAuthUserInfo


@dataclass(frozen=True)
class GithubStrategy:
    settings: Settings

    @property
    def provider(self) -> OAuthProvider:
        return OAuthProvider.GITHUB

    @property
    def authorize_url(self) -> str:
        return "https://github.com/login/oauth/authorize"

    @property
    def token_url(self) -> str:
        return "https://github.com/login/oauth/access_token"

    @property
    def userinfo_url(self) -> str:
        return "https://api.github.com/user"

    @property
    def scope(self) -> str:
        return "read:user user:email"

    @property
    def client_id(self) -> str | None:
        return self.settings.github_oauth_client_id

    @property
    def client_secret(self) -> str | None:
        return self.settings.github_oauth_client_secret

    async def fetch_userinfo(self, client: httpx.AsyncClient, access_token: str) -> OAuthUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        resp = await client.get(self.userinfo_url, headers=headers)
        resp.raise_for_status()
        user = resp.json()

        email = user.get("email")
        if not email:
            # User has private email — fetch /user/emails and pick the primary verified
            email_resp = await client.get("https://api.github.com/user/emails", headers=headers)
            email_resp.raise_for_status()
            emails = email_resp.json()
            primary = next(
                (e["email"] for e in emails if e.get("primary") and e.get("verified")),
                None,
            )
            if primary is None:
                raise httpx.HTTPError("github user has no verified primary email")
            email = primary

        return OAuthUserInfo(
            provider_user_id=str(user["id"]),
            email=email,
            name=user.get("name") or user.get("login") or email.split("@")[0],
            avatar_url=user.get("avatar_url"),
        )
