"""Tests for Stripe-style webhook signing and verification."""

from datetime import UTC, datetime

import pytest

from glossa.ingest.webhook_delivery import fire
from glossa.models.webhook import WebhookEvent
from glossa.webhooks.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    SignatureError,
    sign_payload,
    verify_signature,
)


def test_sign_payload_deterministic():
    secret = "shh"
    body = b'{"event":"job.complete"}'
    ts = 1_700_000_000
    ts1, sig1 = sign_payload(secret=secret, body=body, timestamp=ts)
    ts2, sig2 = sign_payload(secret=secret, body=body, timestamp=ts)
    assert ts1 == ts2 == ts
    assert sig1 == sig2


def test_verify_signature_accepts_own_signature():
    secret = "shh"
    body = b'{"hello":"world"}'
    ts, sig = sign_payload(secret=secret, body=body, timestamp=1_700_000_000)
    verify_signature(
        payload=body,
        signature_header=f"t={ts},v1={sig}",
        secret=secret,
        now=ts,
    )


def test_verify_signature_rejects_wrong_secret():
    body = b'{"hello":"world"}'
    ts, sig = sign_payload(secret="right", body=body, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_signature(
            payload=body,
            signature_header=f"t={ts},v1={sig}",
            secret="wrong",
            now=ts,
        )


def test_verify_signature_rejects_tampered_body():
    secret = "shh"
    body = b'{"hello":"world"}'
    ts, sig = sign_payload(secret=secret, body=body, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_signature(
            payload=b'{"hello":"WORLD"}',
            signature_header=f"t={ts},v1={sig}",
            secret=secret,
            now=ts,
        )


def test_verify_signature_rejects_expired_timestamp():
    secret = "shh"
    body = b"{}"
    ts, sig = sign_payload(secret=secret, body=body, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match="timestamp outside tolerance"):
        verify_signature(
            payload=body,
            signature_header=f"t={ts},v1={sig}",
            secret=secret,
            now=ts + DEFAULT_TOLERANCE_SECONDS + 1,
        )


def test_verify_signature_rejects_future_timestamp():
    secret = "shh"
    body = b"{}"
    ts, sig = sign_payload(secret=secret, body=body, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match="timestamp outside tolerance"):
        verify_signature(
            payload=body,
            signature_header=f"t={ts},v1={sig}",
            secret=secret,
            now=ts - DEFAULT_TOLERANCE_SECONDS - 1,
        )


def test_verify_signature_rejects_missing_t():
    with pytest.raises(SignatureError, match="missing t= or v1="):
        verify_signature(
            payload=b"{}",
            signature_header="v1=abc",
            secret="shh",
            now=1_700_000_000,
        )


def test_verify_signature_rejects_missing_v1():
    with pytest.raises(SignatureError, match="missing t= or v1="):
        verify_signature(
            payload=b"{}",
            signature_header="t=1700000000",
            secret="shh",
            now=1_700_000_000,
        )


def test_verify_signature_rejects_non_integer_timestamp():
    with pytest.raises(SignatureError, match="non-integer timestamp"):
        verify_signature(
            payload=b"{}",
            signature_header="t=not-a-number,v1=abc",
            secret="shh",
            now=1_700_000_000,
        )


def test_verify_signature_handles_whitespace_in_header():
    secret = "shh"
    body = b'{"hello":"world"}'
    ts, sig = sign_payload(secret=secret, body=body, timestamp=1_700_000_000)
    # Stripe occasionally formats headers with a space after the comma.
    verify_signature(
        payload=body,
        signature_header=f"t={ts}, v1={sig}",
        secret=secret,
        now=ts,
    )


async def test_delivery_header_round_trips(monkeypatch, mongomock_db):
    secret = "hook_secret_xyz"
    await mongomock_db.webhooks.insert_one(
        {
            "id": "wh_test",
            "space_id": "gls_test",
            "url": "https://example.test/hook",
            "events": [WebhookEvent.JOB_COMPLETE.value],
            "secret": secret,
            "active": True,
            "created_at": datetime.now(UTC),
        }
    )

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, content, headers):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers

            class R:
                status_code = 200

            return R()

    monkeypatch.setattr("glossa.ingest.webhook_delivery.httpx.AsyncClient", FakeClient)

    await fire(
        space_id="gls_test",
        event=WebhookEvent.JOB_COMPLETE,
        payload={"hello": "world"},
    )

    assert captured["url"] == "https://example.test/hook"
    assert captured["headers"]["X-Glossa-Event"] == WebhookEvent.JOB_COMPLETE.value
    assert captured["headers"]["X-Glossa-Signature"].startswith("t=")
    assert ",v1=" in captured["headers"]["X-Glossa-Signature"]

    verify_signature(
        payload=captured["content"],
        signature_header=captured["headers"]["X-Glossa-Signature"],
        secret=secret,
    )
