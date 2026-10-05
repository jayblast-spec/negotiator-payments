"""
Thin REST client for PayPal's developer platform — no heavy SDK, same
fetch-based style used throughout ArkNet's other hackathon builds
(negotiator-agent, negotiator-mcp-server).

Uses the Payouts API (https://developer.paypal.com/docs/api/payments.payouts-batch/v1/)
to actually pay a vendor once a negotiation is settled — this is the literal
"PayPal developer platform" integration the hackathon rules require.
"""

from __future__ import annotations

import os
import uuid

import httpx

PAYPAL_API_BASE = os.environ.get("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")


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


def create_payout(recipient_email: str, amount: float, currency: str, note: str) -> dict:
    """Pay a vendor directly via PayPal Payouts — the settlement step a
    negotiation agent can't currently do on its own."""
    token = _get_access_token()
    batch_id = str(uuid.uuid4())
    body = {
        "sender_batch_header": {
            "sender_batch_id": batch_id,
            "email_subject": "Payment for negotiated deal",
            "email_message": note,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": f"{amount:.2f}", "currency": currency},
                "receiver": recipient_email,
                "note": note,
                "sender_item_id": batch_id,
            }
        ],
    }
    response = httpx.post(
        f"{PAYPAL_API_BASE}/v1/payments/payouts",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()
