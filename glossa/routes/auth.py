"""OAuth endpoints. Public — no Authorization header required."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from glossa.models.user import OAuthProvider
from glossa.oauth.flow import begin_oauth, complete_oauth
from glossa.sessions import (
    clear_session_cookie,
    create_session,
    destroy_session,
    set_session_cookie,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _resolve_post_login_redirect(state_redirect_to: str | None) -> str:
    """Choose where to send the user after OAuth callback success.

    Only honors ``state_redirect_to`` if it's an internal path (starts with ``/``
    and contains no scheme separator ``://``). Falls back to ``/dashboard/``
    otherwise. Prevents open-redirect attacks where an attacker crafts an
    /auth/{provider}/start?redirect_to=http://evil.com link.
    """
    if state_redirect_to and state_redirect_to.startswith("/") and "://" not in state_redirect_to:
        return state_redirect_to
    return "/dashboard/"


@router.get("/{provider}/start")
async def start_oauth(
    provider: str,
    request: Request,
    redirect_to: str | None = None,
) -> RedirectResponse:
    try:
        prov = OAuthProvider(provider)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="unknown provider") from e
    settings = request.app.state.settings
    try:
        result = await begin_oauth(provider=prov, settings=settings, redirect_to=redirect_to)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return RedirectResponse(url=result.authorize_url, status_code=303)


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    try:
        prov = OAuthProvider(provider)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="unknown provider") from e
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")
    settings = request.app.state.settings
    try:
        result = await complete_oauth(
            provider=prov,
            settings=settings,
            code=code,
            state_id=state,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    session = await create_session(
        user_id=result.user.id,
        ttl_hours=settings.session_ttl_hours,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    dest = _resolve_post_login_redirect(result.redirect_to)
    response = RedirectResponse(url=dest, status_code=303)
    set_session_cookie(response, session_id=session.id, settings=settings)
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        await destroy_session(cookie)
    response = RedirectResponse(url="/dashboard/login", status_code=303)
    clear_session_cookie(response, settings=settings)
    return response
