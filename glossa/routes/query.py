from fastapi import APIRouter, HTTPException, Request

from glossa.db.client import get_db
from glossa.query import QueryRequest, QueryResponse, answer_question
from glossa.usage.quota import QuotaExceededError, check_quota

router = APIRouter(prefix="/spaces/{space_id}/query", tags=["query"])


@router.post("", response_model=QueryResponse)
async def post_query(space_id: str, body: QueryRequest, request: Request) -> QueryResponse:
    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id}, {"id": 1, "tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    try:
        await check_quota(space_doc["tenant_id"])
    except QuotaExceededError as e:
        raise HTTPException(status_code=402, detail={"reason": e.reason, "quota": e.status.model_dump()}) from e
    return await answer_question(
        space_id=space_id,
        request=body,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
    )
