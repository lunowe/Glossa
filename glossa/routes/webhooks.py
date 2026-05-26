from datetime import UTC, datetime
from secrets import token_urlsafe
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from glossa.db.client import get_db
from glossa.models.webhook import Webhook, WebhookCreate

router = APIRouter(prefix="/spaces/{space_id}/webhooks", tags=["webhooks"])


@router.post("", response_model=Webhook)
async def create_webhook(space_id: str, body: WebhookCreate) -> Webhook:
    db = get_db()
    if not await db.spaces.find_one({"id": space_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    webhook = Webhook(
        id=f"wh_{uuid4().hex[:12]}",
        space_id=space_id,
        url=body.url,
        events=body.events,
        secret=body.secret or token_urlsafe(32),
        active=True,
        created_at=datetime.now(UTC),
    )
    await db.webhooks.insert_one(webhook.model_dump())
    return webhook


@router.get("", response_model=list[Webhook])
async def list_webhooks(space_id: str) -> list[Webhook]:
    db = get_db()
    cursor = db.webhooks.find({"space_id": space_id})
    return [Webhook.model_validate(doc) async for doc in cursor]


@router.delete("/{webhook_id}")
async def delete_webhook(space_id: str, webhook_id: str) -> dict:
    db = get_db()
    result = await db.webhooks.delete_one({"id": webhook_id, "space_id": space_id})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="webhook not found")
    return {"ok": True}
