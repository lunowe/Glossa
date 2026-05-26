from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PageKind(StrEnum):
    ENTITY = "entity"
    TOPIC = "topic"
    SUMMARY = "summary"
    SYNTHESIS = "synthesis"
    COMPARISON = "comparison"
    SYSTEM = "system"
    CUSTOM = "custom"


class Page(BaseModel):
    space_id: str
    path: str
    kind: PageKind
    title: str
    frontmatter: dict = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    backlinks: list[str] = Field(default_factory=list)
    updated_at: datetime
    last_touched_by_job_id: str | None = None


class PageWithContent(Page):
    content: str
