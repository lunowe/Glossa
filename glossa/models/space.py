from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LLMMode(StrEnum):
    HOSTED = "hosted"
    BYO = "byo"


class LLMConfig(BaseModel):
    mode: LLMMode = LLMMode.BYO
    endpoint: str | None = None
    model: str | None = None
    api_key_ref: str | None = None
    extra: dict = Field(default_factory=dict)


class SpaceStats(BaseModel):
    source_count: int = 0
    page_count: int = 0
    last_ingest_at: datetime | None = None


class Space(BaseModel):
    id: str
    tenant_id: str
    name: str
    slug: str
    bucket_uri: str
    schema_path: str = "schema.md"
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    stats: SpaceStats = Field(default_factory=SpaceStats)
    created_at: datetime
    updated_at: datetime


class SpaceCreate(BaseModel):
    tenant_id: str
    name: str
    slug: str | None = None
    llm_config: LLMConfig | None = None
    schema_markdown: str | None = None


class SpaceUpdate(BaseModel):
    name: str | None = None
    llm_config: LLMConfig | None = None
