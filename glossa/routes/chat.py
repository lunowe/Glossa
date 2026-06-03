from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from glossa.auth import AuthContext, get_auth_context, space_query
from glossa.chat import ChatRequest, ChatResponse, answer_chat, chat_event_stream, streaming_response
from glossa.db.client import get_db
from glossa.usage.quota import QuotaExceededError, check_quota

router = APIRouter(prefix="/spaces/{space_id}/chat", tags=["chat"])


async def _check_space_and_quota(space_id: str, ctx: AuthContext) -> None:
    db = get_db()
    space_doc = await db.spaces.find_one(space_query(space_id, ctx), {"id": 1, "tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    try:
        await check_quota(space_doc["tenant_id"])
    except QuotaExceededError as e:
        raise HTTPException(status_code=402, detail={"reason": e.reason, "quota": e.status.model_dump()}) from e


@router.post("", response_model=ChatResponse)
async def post_chat(
    space_id: str,
    body: ChatRequest,
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> ChatResponse:
    await _check_space_and_quota(space_id, ctx)
    return await answer_chat(
        space_id=space_id,
        request=body,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
    )


@router.post("/stream")
async def post_chat_stream(
    space_id: str,
    body: ChatRequest,
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
):
    await _check_space_and_quota(space_id, ctx)
    return streaming_response(
        chat_event_stream(
            space_id=space_id,
            request=body,
            storage=request.app.state.storage,
            settings=request.app.state.settings,
        )
    )
