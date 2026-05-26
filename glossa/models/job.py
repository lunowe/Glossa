from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobKind(StrEnum):
    INGEST = "ingest"
    LINT = "lint"
    REINDEX = "reindex"
    REBUILD_INDEX = "rebuild_index"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobResult(BaseModel):
    pages_created: list[str] = Field(default_factory=list)
    pages_updated: list[str] = Field(default_factory=list)
    contradictions_flagged: list[dict] = Field(default_factory=list)
    lint_findings: list[dict] = Field(default_factory=list)
    lint_summary: dict[str, int] = Field(default_factory=dict)
    log_entry: str | None = None


class Job(BaseModel):
    id: str
    space_id: str
    kind: JobKind
    inputs: dict = Field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    result: JobResult | None = None
    webhook_url: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
