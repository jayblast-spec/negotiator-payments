"""
Closes the loop that AI-NEGOTIATOR and negotiator-agent leave open today:
both produce a ranked vendor + agreed price + negotiation script, but a
human still has to go pay the vendor themselves. This is the "Agentic
Commerce" step — an agent that negotiates AND settles the payment.

Reuses the same Groq model/provider every prior ArkNet hackathon build has
used (openai/gpt-oss-20b), here to turn the deal terms into a short payout
memo, then calls PayPal Payouts (paypal_client.py) to actually pay.
"""

from __future__ import annotations

import os

import httpx

from paypal_client import create_payout

GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _draft_payout_memo(vendor: str, purchase_request: str, agreed_price: float, currency: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — reuse the same key AI-NEGOTIATOR's Edge Function uses.")
    prompt = (
        f"Write a short (one sentence) payment memo for a PayPal payout to a vendor "
        f"named {vendor!r}, settling a negotiated purchase of {purchase_request!r} "
        f"for {agreed_price} {currency}. Plain text only, no markdown, no preamble."
    )
    response = httpx.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "reasoning_effort": "low",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def settle_negotiated_deal(
    vendor: str,
    vendor_paypal_email: str,
    purchase_request: str,
    agreed_price: float,
    currency: str = "USD",
) -> dict:
    """Agentic-commerce settlement: draft the payout memo with the same AI
    model the negotiation itself used, then actually pay the vendor.

    This is the real new capability this hackathon adds on top of
    AI-NEGOTIATOR's existing rank-and-negotiate flow: taking action, not
    just producing a recommendation a human has to execute by hand.
    """
    memo = _draft_payout_memo(vendor, purchase_request, agreed_price, currency)
    payout = create_payout(vendor_paypal_email, agreed_price, currency, memo)
    return {"memo": memo, "payout": payout}


if __name__ == "__main__":
    import json

    result = settle_negotiated_deal(
        vendor=os.environ.get("DEMO_VENDOR", "Example Vendor Co"),
        vendor_paypal_email=os.environ.get("DEMO_VENDOR_PAYPAL_EMAIL", "vendor@example.com"),
        purchase_request=os.environ.get("DEMO_PURCHASE_REQUEST", "500 units of packaging tape"),
        agreed_price=float(os.environ.get("DEMO_AGREED_PRICE", "250.00")),
    )
    print(json.dumps(result, indent=2))
