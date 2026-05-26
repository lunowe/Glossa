"""Records one RequestEvent per HTTP request.

Runs the request, captures (method, path, status, duration_ms), reads
tenant_id and api_key_id from request.state.auth (set by the auth
dependency), and persists the event fire-and-forget. /healthz is skipped.
"""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from glossa.activity.recorder import record_request

SKIP_PATHS = {"/healthz"}


class ActivityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in SKIP_PATHS:
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
            error = None if status_code < 500 else "server_error"
        except Exception:
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._record(request, status_code=500, duration_ms=duration_ms, error="server_error")
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)
        await self._record(
            request,
            status_code=status_code,
            duration_ms=duration_ms,
            error=error,
        )
        return response

    @staticmethod
    async def _record(request: Request, *, status_code: int, duration_ms: int, error: str | None) -> None:
        auth = getattr(request.state, "auth", None)
        tenant_id = getattr(auth, "tenant_id", None) if auth else None
        api_key_id = getattr(auth, "api_key_id", None) if auth else None
        await record_request(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=duration_ms,
            error=error,
        )
