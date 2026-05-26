"""Thin async HTTP wrapper around the Glossa API for the MCP server.

Each method maps to one Glossa endpoint and returns the parsed JSON. The
client is shared across all MCP tool calls in one server process.

Auth: an optional bearer token (``GLOSSA_API_TOKEN``) is forwarded on every
request so the wrapper is forward-compatible with the auth layer Glossa will
add. Today Glossa accepts unauthenticated calls; the header is harmless.
"""

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 60.0


class GlossaClientError(RuntimeError):
    """Raised when the Glossa API returns a non-2xx response."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"Glossa API error {status}: {body!r}")
        self.status = status
        self.body = body


class GlossaClient:
    """Async client for the Glossa HTTP API.

    Use as an async context manager so the underlying httpx connection pool
    is released on shutdown:

        async with GlossaClient.from_env() as client:
            ...
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        default_space_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._default_space_id = default_space_id
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._owns_client = client is None

    @classmethod
    def from_env(cls, *, client: httpx.AsyncClient | None = None) -> "GlossaClient":
        base_url = os.environ.get("GLOSSA_BASE_URL", "http://localhost:8200")
        return cls(
            base_url=base_url,
            api_token=os.environ.get("GLOSSA_API_TOKEN") or None,
            default_space_id=os.environ.get("GLOSSA_DEFAULT_SPACE_ID") or None,
            client=client,
        )

    @property
    def default_space_id(self) -> str | None:
        return self._default_space_id

    def resolve_space_id(self, space_id: str | None) -> str:
        resolved = space_id or self._default_space_id
        if not resolved:
            raise ValueError(
                "No space_id provided and GLOSSA_DEFAULT_SPACE_ID is not set. "
                "Pass space_id explicitly or configure a default."
            )
        return resolved

    async def __aenter__(self) -> "GlossaClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        url = f"{self._base_url}{path}"
        resp = await self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            raise GlossaClientError(resp.status_code, body)
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    # --- spaces -----------------------------------------------------------

    async def list_spaces(self, tenant_id: str | None = None) -> list[dict]:
        params: dict = {}
        if tenant_id:
            params["tenant_id"] = tenant_id
        return await self._request("GET", "/spaces", params=params)

    async def get_space(self, space_id: str) -> dict:
        return await self._request("GET", f"/spaces/{space_id}")

    async def get_schema(self, space_id: str) -> dict:
        return await self._request("GET", f"/spaces/{space_id}/schema")

    # --- pages ------------------------------------------------------------

    async def list_pages(
        self,
        space_id: str,
        *,
        kind: str | None = None,
        path_prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if kind:
            params["kind"] = kind
        if path_prefix:
            params["path_prefix"] = path_prefix
        return await self._request("GET", f"/spaces/{space_id}/pages", params=params)

    async def get_page(self, space_id: str, path: str) -> dict:
        return await self._request("GET", f"/spaces/{space_id}/pages/{path}")

    async def get_index(self, space_id: str) -> dict:
        return await self._request("GET", f"/spaces/{space_id}/index")

    async def get_log(self, space_id: str, *, tail: int | None = None) -> dict:
        params: dict = {}
        if tail:
            params["tail"] = tail
        return await self._request("GET", f"/spaces/{space_id}/log", params=params)

    async def get_lint_report(self, space_id: str) -> dict:
        return await self._request("GET", f"/spaces/{space_id}/lint-report")

    # --- sources ----------------------------------------------------------

    async def create_source(
        self,
        space_id: str,
        *,
        title: str,
        content: str | None = None,
        external_uri: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        payload: dict = {
            "title": title,
            "ingestion_mode": "push",
            "content_inline": content,
            "external_uri": external_uri,
            "metadata": metadata or {},
        }
        return await self._request("POST", f"/spaces/{space_id}/sources", json=payload)

    async def ingest_source(self, space_id: str, source_id: str) -> dict:
        return await self._request(
            "POST",
            f"/spaces/{space_id}/sources/{source_id}/ingest",
        )

    # --- jobs -------------------------------------------------------------

    async def get_job(self, job_id: str) -> dict:
        return await self._request("GET", f"/jobs/{job_id}")

    # --- query / lint -----------------------------------------------------

    async def query(self, space_id: str, *, question: str, max_pages: int = 8) -> dict:
        return await self._request(
            "POST",
            f"/spaces/{space_id}/query",
            json={"question": question, "max_pages": max_pages},
        )

    async def lint(self, space_id: str) -> dict:
        return await self._request("POST", f"/spaces/{space_id}/lint")
