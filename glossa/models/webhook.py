from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class WebhookEvent(StrEnum):
    JOB_COMPLETE = "job.complete"
    JOB_FAILED = "job.failed"
    PAGE_UPDATED = "page.updated"
    PAGE_CREATED = "page.created"
    SOURCE_RECEIVED = "source.received"


class Webhook(BaseModel):
    id: str
    space_id: str
    url: str
    events: list[WebhookEvent]
    secret: str
    active: bool = True
    created_at: datetime


class WebhookCreate(BaseModel):
    url: str
    events: list[WebhookEvent]
    secret: str | None = None


class WebhookDelivery(BaseModel):
    event: WebhookEvent
    space_id: str
    payload: dict = Field(default_factory=dict)
    delivered_at: datetime
