"""Fire registered webhooks for a Space.

Best-effort: delivery failures are logged but do not fail the calling job.
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime

import httpx

from glossa.db.client import get_db
from glossa.models.webhook import Webhook, WebhookEvent

logger = logging.getLogger(__name__)


async def fire(*, space_id: str, event: WebhookEvent, payload: dict) -> None:
    db = get_db()
    cursor = db.webhooks.find({"space_id": space_id, "active": True, "events": event.value})
    hooks = [Webhook.model_validate(doc) async for doc in cursor]
    if not hooks:
        return

    body = json.dumps(
        {
            "event": event.value,
            "space_id": space_id,
            "delivered_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        },
        sort_keys=True,
    ).encode("utf-8")

    async with httpx.AsyncClient(timeout=10.0) as client:
        for hook in hooks:
            timestamp = int(time.time())
            signed_payload = f"{timestamp}.".encode() + body
            signature = hmac.new(
                hook.secret.encode("utf-8"),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-Glossa-Event": event.value,
                "X-Glossa-Signature": f"t={timestamp},v1={signature}",
            }
            try:
                resp = await client.post(hook.url, content=body, headers=headers)
                if resp.status_code >= 400:
                    logger.warning("webhook %s returned %s", hook.id, resp.status_code)
            except httpx.HTTPError as e:
                logger.warning("webhook %s delivery failed: %s", hook.id, e)
