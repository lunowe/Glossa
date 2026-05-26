"""Verify Stripe-style HMAC signatures on inbound Glossa webhooks.

Usage from an integrator's server:

    from glossa.webhooks.signing import SignatureError, verify_signature

    try:
        verify_signature(
            payload=request_body_bytes,
            signature_header=request.headers["X-Glossa-Signature"],
            secret=webhook_secret,
        )
    except SignatureError:
        return Response(status_code=400)
"""

import hashlib
import hmac
import time

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """Raised when a webhook signature is malformed, expired, or wrong."""


def sign_payload(*, secret: str, body: bytes, timestamp: int | None = None) -> tuple[int, str]:
    """Compute (timestamp, hex_signature) for a Stripe-style v1 signature."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return ts, sig


def verify_signature(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> None:
    """Raise SignatureError if the header is invalid for the payload+secret.

    Accepts a Stripe-style header: ``t=<unix>,v1=<hex>``. Future schemes
    can add more ``vN=`` entries; v1 must verify today.
    """
    parsed = _parse(signature_header)
    if "t" not in parsed or "v1" not in parsed:
        raise SignatureError("missing t= or v1= in signature header")
    try:
        ts = int(parsed["t"])
    except ValueError as e:
        raise SignatureError("non-integer timestamp") from e

    current = now if now is not None else int(time.time())
    if abs(current - ts) > tolerance_seconds:
        raise SignatureError(f"timestamp outside tolerance ({abs(current - ts)}s)")

    signed = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parsed["v1"]):
        raise SignatureError("signature mismatch")


def _parse(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out
