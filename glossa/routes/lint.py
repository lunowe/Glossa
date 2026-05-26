from fastapi import APIRouter, HTTPException, Request

from glossa.db.client import get_db
from glossa.lint.workflow import enqueue_lint
from glossa.models.job import Job
from glossa.usage.quota import QuotaExceededError, check_quota

router = APIRouter(prefix="/spaces/{space_id}/lint", tags=["lint"])


@router.post("", response_model=Job)
async def post_lint(space_id: str, request: Request) -> Job:
    db = get_db()
    space_doc = await db.spaces.find_one({"id": space_id}, {"tenant_id": 1})
    if not space_doc:
        raise HTTPException(status_code=404, detail="space not found")
    try:
        await check_quota(space_doc["tenant_id"])
    except QuotaExceededError as e:
        raise HTTPException(
            status_code=402,
            detail={"reason": e.reason, "quota": e.status.model_dump()},
        ) from e
    return await enqueue_lint(space_id=space_id, app=request.app)
