from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from glossa.auth import AuthContext, get_auth_context, space_query
from glossa.db.client import get_db
from glossa.models.job import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=Job)
async def get_job(
    job_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Job:
    db = get_db()
    doc = await db.jobs.find_one({"id": job_id})
    if not doc:
        raise HTTPException(status_code=404, detail="job not found")
    # Tenant ownership flows through the job's space.
    if not await db.spaces.find_one(space_query(doc["space_id"], ctx), {"id": 1}):
        raise HTTPException(status_code=404, detail="job not found")
    return Job.model_validate(doc)
