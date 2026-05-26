from fastapi import APIRouter, HTTPException

from glossa.db.client import get_db
from glossa.models.job import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    db = get_db()
    doc = await db.jobs.find_one({"id": job_id})
    if not doc:
        raise HTTPException(status_code=404, detail="job not found")
    return Job.model_validate(doc)
