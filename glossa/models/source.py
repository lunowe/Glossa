from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class SourceIngestionMode(StrEnum):
    PUSH = "push"
    PULL = "pull"
    URL = "url"
    UPLOAD = "upload"


class SourceStatus(StrEnum):
    RECEIVED = "received"
    INGESTING = "ingesting"
    DONE = "done"
    FAILED = "failed"


class FetchCallback(BaseModel):
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    auth_ref: str | None = None


class Source(BaseModel):
    id: str
    space_id: str
    title: str
    ingestion_mode: SourceIngestionMode
    content_inline: str | None = None
    fetch_callback: FetchCallback | None = None
    external_uri: str | None = None
    # For ``upload`` sources: storage-relative path of the raw uploaded file,
    # e.g. ``assets/src-<id>/report.pdf``. Parsed to text during ingest.
    asset_path: str | None = None
    metadata: dict = Field(default_factory=dict)
    status: SourceStatus = SourceStatus.RECEIVED
    created_at: datetime
    last_ingested_at: datetime | None = None
    last_ingest_job_id: str | None = None


class SourceCreate(BaseModel):
    title: str
    ingestion_mode: SourceIngestionMode
    content_inline: str | None = None
    fetch_callback: FetchCallback | None = None
    external_uri: str | None = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_mode_content(self) -> "SourceCreate":
        if self.ingestion_mode == SourceIngestionMode.PUSH and not self.content_inline:
            raise ValueError("push mode requires content_inline")
        if self.ingestion_mode == SourceIngestionMode.PULL and not self.fetch_callback:
            raise ValueError("pull mode requires fetch_callback")
        if self.ingestion_mode == SourceIngestionMode.URL and not self.external_uri:
            raise ValueError("url mode requires external_uri (the link to fetch)")
        if self.ingestion_mode == SourceIngestionMode.UPLOAD:
            raise ValueError("upload sources are created via POST /spaces/{space_id}/sources/upload")
        return self
