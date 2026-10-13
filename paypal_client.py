"""
Thin REST client for PayPal's developer platform: OAuth2 client-credentials
plus the Payouts API. No SDK, just httpx.

Docs this follows:
- https://developer.paypal.com/api/rest/authentication/
- https://developer.paypal.com/docs/payouts/standard/integrate-api/
- https://developer.paypal.com/docs/api/payments.payouts-batch/v1/

Idempotency: PayPal's Payouts guide says to retry after a server error by
sending the request again with the same `sender_batch_id`. `create_payout`
therefore fixes one `sender_batch_id` per call and reuses it, byte for byte,
on every retry. Callers that may retry across processes (settle_deal.py does)
pass their own deterministic `sender_batch_id`.
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urlparse

import httpx

PAYPAL_API_BASE = os.environ.get("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")

_SANDBOX_HOSTS = {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
_RETRY_BACKOFF_SECONDS = (0.5, 1.5)

# Indirection so tests can skip the backoff without patching the time module.
_sleep = time.sleep


class PayoutOutcomeUnknown(Exception):
    """PayPal gave no definite answer about whether the payout batch exists.

    Raised when a retry is refused with a 4xx after an earlier attempt timed
    out or hit a 5xx: the refusal may mean the first attempt went through
    (a duplicate sender_batch_id), so the payout must not be assumed unpaid.
    """


def _assert_sandbox(base_url: str) -> None:
    """This project moves money on an agent's say-so, so it refuses to talk to
    live PayPal unless a human opts in explicitly."""
    host = (urlparse(base_url).hostname or "").lower()
    if host in _SANDBOX_HOSTS or host in _LOCAL_HOSTS:
        return
    if os.environ.get("PAYPAL_ALLOW_LIVE") == "1":
        return
    raise RuntimeError(
        f"PAYPAL_API_BASE host {host!r} is not the PayPal sandbox. "
        "Refusing to send payouts. Set PAYPAL_ALLOW_LIVE=1 to override."
    )


def _get_access_token() -> str:
    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set.")
    response = httpx.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def build_payout_body(
    recipient_email: str,
    amount: float,
    currency: str,
    note: str,
    sender_batch_id: str,
) -> dict:
    """The exact JSON body sent to POST /v1/payments/payouts. Kept separate so
    the dry-run demo can show it without sending anything."""
    return {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": "Payment for negotiated deal",
            "email_message": note,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": f"{amount:.2f}", "currency": currency},
                "receiver": recipient_email,
                "note": note,
                "sender_item_id": sender_batch_id,
            }
        ],
    }


def create_payout(
    recipient_email: str,
    amount: float,
    currency: str,
    note: str,
    sender_batch_id: str | None = None,
    max_attempts: int = 3,
) -> dict:
    """Pay a vendor directly via PayPal Payouts.

    One `sender_batch_id` is used for every attempt made by this call, so a
    retry after a timeout or a 5xx cannot create a second payout. Client
    errors (4xx) are never retried.
    """
    _assert_sandbox(PAYPAL_API_BASE)
    batch_id = sender_batch_id or str(uuid.uuid4())
    body = build_payout_body(recipient_email, amount, currency, note, batch_id)
    token = _get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": batch_id,
    }

    attempts = max(1, max_attempts)
    earlier_attempt_unresolved = False
    for attempt in range(1, attempts + 1):
        last_attempt = attempt == attempts
        try:
            response = httpx.post(
                f"{PAYPAL_API_BASE}/v1/payments/payouts",
                json=body,
                headers=headers,
                timeout=30.0,
            )
        except httpx.TransportError:
            # Outcome unknown (timeout, dropped connection). Retrying with the
            # same sender_batch_id is the documented safe path.
            if last_attempt:
                raise
            earlier_attempt_unresolved = True
        else:
            if 400 <= response.status_code < 500 and earlier_attempt_unresolved:
                raise PayoutOutcomeUnknown(
                    f"PayPal answered HTTP {response.status_code} to a retry of batch {batch_id}; "
                    "an earlier attempt may have created it"
                )
            if response.status_code < 500 or last_attempt:
                response.raise_for_status()
                return response.json()
            earlier_attempt_unresolved = True
        _sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])

    raise RuntimeError("unreachable: payout retry loop exited without a result")
