"""
Settles a negotiated deal: AI-NEGOTIATOR and negotiator-agent produce a
ranked vendor, an agreed price and a negotiation script, but a human still
has to go and pay. This module is the settlement step.

Order of operations, and why:

1. spend_approval.authorize_payment() checks the intended payment against the
   spending policy. Anything other than "allow" stops here: no memo is
   drafted and PayPal is never called.
2. Groq (openai/gpt-oss-20b) drafts a one-sentence payout memo. The model
   writes words only. The amount and the recipient come from the approved
   payment, never from model output. If Groq is not configured or fails, a
   plain template memo is used so an approved payment is not lost to a memo.
3. paypal_client.create_payout() sends the payout with a sender_batch_id
   derived from the approved payment, so a retry cannot pay twice.
4. The result is appended to the same tamper-evident log as the decision.
"""

from __future__ import annotations

import os

import httpx

import spend_approval
from paypal_client import build_payout_body, create_payout

GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MEMO_MAX_CHARS = 400


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


def _template_memo(vendor: str, purchase_request: str, agreed_price: object, currency: str) -> str:
    return f"Payment of {agreed_price} {currency} to {vendor} for {purchase_request}, as agreed."


def _memo(vendor: str, purchase_request: str, agreed_price: object, currency: str) -> tuple:
    """(memo, source). source is "groq" or says why the template was used."""
    template = _template_memo(vendor, purchase_request, agreed_price, currency)
    if not os.environ.get("GROQ_API_KEY"):
        return template, "template (GROQ_API_KEY not set)"
    try:
        drafted = _draft_payout_memo(vendor, purchase_request, float(agreed_price), currency)
    except Exception as exc:
        return template, f"template (Groq call failed: {type(exc).__name__})"
    memo = " ".join(str(drafted).split())[:MEMO_MAX_CHARS]
    if not memo:
        return template, "template (Groq returned an empty memo)"
    return memo, "groq"


def settle_negotiated_deal(
    vendor: str,
    vendor_paypal_email: str,
    purchase_request: str,
    agreed_price: float,
    currency: str = "USD",
    *,
    deal_id: str | None = None,
    dry_run: bool = False,
    policy_path: str | None = None,
    log_path: str | None = None,
) -> dict:
    """Check the payment against the spending policy, then pay the vendor.

    Returns a dict with:
      status   paid | dry_run | denied | needs_approval | already_paid |
               payout_failed | payout_unknown
      decision the spend-approval decision (outcome, reason, backend, ...)
      memo, memo_source, payout, payout_request, sender_batch_id

    Settling the same deal again (same vendor, recipient, amount, currency,
    purpose, or the same deal_id) reports already_paid instead of paying
    twice. Pass a distinct deal_id for a genuinely separate identical payment.

    dry_run=True runs the whole decision path and builds the exact PayPal
    request body, but sends nothing to PayPal.
    """
    decision = spend_approval.authorize_payment(
        vendor,
        vendor_paypal_email,
        agreed_price,
        currency,
        purchase_request,
        deal_id=deal_id,
        policy_path=policy_path,
        log_path=log_path,
        dry_run=dry_run,
    )
    result: dict = {
        "status": "denied",
        "decision": decision.to_dict(),
        "memo": None,
        "memo_source": None,
        "payout": None,
        "payout_request": None,
        "sender_batch_id": None,
    }
    intent = decision.intent

    # Positive check only: the single way past this line is an explicit allow.
    if decision.outcome != spend_approval.ALLOW or intent is None:
        if decision.outcome == spend_approval.NEEDS_APPROVAL:
            result["status"] = "needs_approval"
        elif decision.already_paid:
            result["status"] = "already_paid"
        return result

    memo, memo_source = _memo(intent.vendor, intent.purpose, intent.amount, intent.currency)
    amount = float(intent.amount)
    body = build_payout_body(intent.recipient_email, amount, intent.currency, memo, intent.sender_batch_id)
    result.update(memo=memo, memo_source=memo_source, payout_request=body, sender_batch_id=intent.sender_batch_id)

    # What is about to be sent must be exactly what was approved.
    item = body["items"][0]
    if (
        item["amount"]["value"] != str(intent.amount)
        or item["amount"]["currency"] != intent.currency
        or item["receiver"] != intent.recipient_email
    ):
        result["status"] = "payout_failed"
        result["error"] = "payout request does not match the approved payment; nothing was sent"
        _record(result, intent, "failed", log_path, detail=result["error"])
        return result

    if dry_run:
        result["status"] = "dry_run"
        _record(result, intent, "dry_run", log_path, detail="dry run: nothing was sent to PayPal")
        return result

    try:
        payout = create_payout(
            intent.recipient_email,
            amount,
            intent.currency,
            memo,
            sender_batch_id=intent.sender_batch_id,
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        # 4xx: PayPal refused the request, so nothing was created. 5xx after
        # retries: PayPal may or may not have created the batch.
        definite = 400 <= code < 500
        result["status"] = "payout_failed" if definite else "payout_unknown"
        result["error"] = f"PayPal answered HTTP {code}"
        _record(result, intent, "failed" if definite else "unknown", log_path, detail=result["error"])
        return result
    except RuntimeError as exc:
        # Raised before any payout request: missing credentials or a non-sandbox host.
        result["status"] = "payout_failed"
        result["error"] = str(exc)[:200]
        _record(result, intent, "failed", log_path, detail=result["error"])
        return result
    except Exception as exc:
        result["status"] = "payout_unknown"
        result["error"] = f"no definite answer from PayPal ({type(exc).__name__})"
        _record(result, intent, "unknown", log_path, detail=result["error"])
        return result

    result["status"] = "paid"
    result["payout"] = payout
    batch_header = payout.get("batch_header") if isinstance(payout, dict) else None
    batch_id = batch_header.get("payout_batch_id") if isinstance(batch_header, dict) else None
    _record(result, intent, "sent", log_path, payout_batch_id=batch_id)
    return result


def _record(result: dict, intent: "spend_approval.PaymentIntent", status: str, log_path: str | None, **fields) -> None:
    """Write the payout outcome to the decision log. A logging failure after
    money moved cannot undo the payout, so it is surfaced, not raised."""
    try:
        record = spend_approval.record_payout(intent, status, log_path=log_path, **fields)
        result["log_head"] = record["hash"]
    except Exception as exc:
        result["log_error"] = f"payout outcome could not be logged: {type(exc).__name__}"


if __name__ == "__main__":
    import json

    live = bool(os.environ.get("PAYPAL_CLIENT_ID"))
    if not live:
        print("DRY RUN: PAYPAL_CLIENT_ID is not set. Nothing will be sent to PayPal.")
    outcome = settle_negotiated_deal(
        vendor=os.environ.get("DEMO_VENDOR", "Example Vendor Co"),
        vendor_paypal_email=os.environ.get("DEMO_VENDOR_PAYPAL_EMAIL", "vendor@example.com"),
        purchase_request=os.environ.get("DEMO_PURCHASE_REQUEST", "500 units of packaging tape"),
        agreed_price=float(os.environ.get("DEMO_AGREED_PRICE", "250.00")),
        dry_run=not live,
    )
    print(json.dumps(outcome, indent=2))
