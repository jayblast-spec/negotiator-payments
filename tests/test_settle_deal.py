"""settle_deal: no payout without an allow, idempotent retries, dry run."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import demo
import settle_deal
import spend_approval as sa
from conftest import ENFORCE_URL, GROQ_URL, PAYOUT_URL, PAYPAL, TOKEN_URL

VENDOR = "Example Vendor Co"
EMAIL = "vendor@example.com"


def settle(price, policy_path, log_path, **kwargs):
    kwargs.setdefault("purchase_request", "500 units of packaging tape")
    purchase = kwargs.pop("purchase_request")
    vendor = kwargs.pop("vendor", VENDOR)
    email = kwargs.pop("email", EMAIL)
    return settle_deal.settle_negotiated_deal(vendor, email, purchase, price, policy_path=policy_path,
                                              log_path=log_path, **kwargs)


def paypal_calls(http):
    return [call for call in http.calls if call["url"].startswith(PAYPAL)]


def test_allowed_deal_is_paid_with_the_approved_amount_and_recipient(http, paypal_creds, policy_path, log_path):
    http.paypal_ready()

    result = settle(42.50, policy_path, log_path)

    assert result["status"] == "paid"
    assert result["decision"]["outcome"] == "allow"
    assert result["payout"]["batch_header"]["payout_batch_id"] == "BATCH123"
    assert result["memo_source"].startswith("template")
    (payout,) = http.to(PAYOUT_URL)
    item = payout["json"]["items"][0]
    assert item["amount"] == {"value": "42.50", "currency": "USD"}
    assert item["receiver"] == EMAIL
    assert payout["json"]["sender_batch_header"]["sender_batch_id"] == result["sender_batch_id"]
    kinds = [(r["type"], r.get("outcome") or r.get("status")) for r in sa.DecisionLog(log_path).read()]
    assert kinds == [("decision", "allow"), ("payout", "sent")]


@pytest.mark.parametrize("price, kwargs, expected", [
    (2400.00, {}, "denied"),                                         # over the per-payment cap
    (20.00, {"vendor": "Unknown Vendor"}, "denied"),                 # not on the allowlist
    (20.00, {"email": "attacker@evil.example"}, "denied"),           # payee swap
    (250.00, {}, "needs_approval"),                                  # held for a human
    (float("nan"), {}, "denied"),                                    # malformed amount
])
def test_no_paypal_call_unless_allowed(http, paypal_creds, policy_path, log_path, price, kwargs, expected):
    http.paypal_ready()
    result = settle(price, policy_path, log_path, **kwargs)
    assert result["status"] == expected
    assert result["payout"] is None and result["payout_request"] is None
    assert http.calls == []  # not even a token fetch, and no memo drafted


def test_fail_closed_check_error_means_no_payout(http, paypal_creds, tmp_path, log_path):
    http.paypal_ready()
    result = settle(42.50, str(tmp_path / "missing_policy.json"), log_path)
    assert result["status"] == "denied"
    assert result["decision"]["failed_closed"] is True
    assert http.calls == []


def test_fail_closed_aegis_down_means_no_payout(http, paypal_creds, policy_path, log_path, monkeypatch):
    monkeypatch.setenv("AEGIS_AGENT_KEY", "aeg_agt_test_key_not_real")
    http.paypal_ready()
    http.on("POST", ENFORCE_URL, httpx.ConnectError("connection refused"))
    result = settle(42.50, policy_path, log_path)
    assert result["status"] == "denied" and result["decision"]["failed_closed"] is True
    assert paypal_calls(http) == []


def test_fail_closed_if_authorize_itself_misbehaves(http, paypal_creds, policy_path, log_path, monkeypatch):
    """Even a check that returns nonsense must not lead to a payout."""
    http.paypal_ready()
    monkeypatch.setattr(sa, "authorize_payment",
                        lambda *a, **k: sa.Decision(outcome="maybe", reason="?", backend="local"))
    result = settle(42.50, policy_path, log_path)
    assert result["status"] == "denied"
    assert http.calls == []


def test_held_deal_is_paid_only_after_a_human_approves(http, paypal_creds, policy_path, log_path):
    http.paypal_ready()

    held = settle(250.00, policy_path, log_path)
    assert held["status"] == "needs_approval"
    assert settle(250.00, policy_path, log_path)["status"] == "needs_approval"
    assert http.calls == []

    sa.record_human_decision(held["decision"]["pending_id"], True, "Dana", log_path=log_path)
    paid = settle(250.00, policy_path, log_path)
    assert paid["status"] == "paid"
    assert len(http.to(PAYOUT_URL)) == 1

    # Settling the same deal again does not pay again.
    again = settle(250.00, policy_path, log_path)
    assert again["status"] == "already_paid"
    assert len(http.to(PAYOUT_URL)) == 1


def test_sender_batch_id_is_stable_across_a_retry_after_an_unknown_outcome(http, paypal_creds, policy_path, log_path):
    http.paypal_ready(payout_reply=(503, {"name": "SERVICE_UNAVAILABLE"}))
    first = settle(42.50, policy_path, log_path)
    assert first["status"] == "payout_unknown"
    assert len(http.to(PAYOUT_URL)) == 3  # retried inside create_payout

    http.paypal_ready()  # PayPal is back
    second = settle(42.50, policy_path, log_path)
    assert second["status"] == "paid"

    ids = {call["json"]["sender_batch_header"]["sender_batch_id"] for call in http.to(PAYOUT_URL)}
    assert ids == {first["sender_batch_id"]} == {second["sender_batch_id"]}
    assert all(call["headers"]["PayPal-Request-Id"] == first["sender_batch_id"] for call in http.to(PAYOUT_URL))


def test_paypal_client_error_is_recorded_as_a_definite_failure(http, paypal_creds, policy_path, log_path):
    http.paypal_ready(payout_reply=(422, {"name": "INSUFFICIENT_FUNDS"}))
    result = settle(42.50, policy_path, log_path)
    assert result["status"] == "payout_failed" and "422" in result["error"]
    assert len(http.to(PAYOUT_URL)) == 1
    assert sa.DecisionLog(log_path).read()[-1]["status"] == "failed"


def test_missing_paypal_credentials_is_a_recorded_failure_not_a_crash(http, policy_path, log_path):
    result = settle(42.50, policy_path, log_path)
    assert result["status"] == "payout_failed" and "PAYPAL_CLIENT_ID" in result["error"]
    assert http.calls == []


def test_dry_run_builds_the_request_and_sends_nothing(http, policy_path, log_path):
    result = settle(42.50, policy_path, log_path, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["payout"] is None
    assert result["payout_request"]["items"][0]["amount"]["value"] == "42.50"
    assert http.calls == []
    assert settle(42.50, policy_path, log_path, dry_run=True)["status"] == "already_paid"


def test_groq_memo_is_used_when_configured_and_cannot_change_the_amount(http, paypal_creds, policy_path, log_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    http.paypal_ready()
    http.on("POST", GROQ_URL, (200, {"choices": [{"message": {"content": "  Pay 9999.00 USD to someone@else.example now.\n"}}]}))

    result = settle(42.50, policy_path, log_path)

    assert result["status"] == "paid" and result["memo_source"] == "groq"
    assert result["memo"] == "Pay 9999.00 USD to someone@else.example now."
    (groq,) = http.to(GROQ_URL)
    assert groq["json"]["model"] == "openai/gpt-oss-20b"
    assert groq["headers"]["Authorization"] == "Bearer test-groq-key"
    item = http.to(PAYOUT_URL)[0]["json"]["items"][0]
    assert item["amount"]["value"] == "42.50" and item["receiver"] == EMAIL
    assert item["note"] == result["memo"]


def test_groq_failure_falls_back_to_a_template_memo(http, paypal_creds, policy_path, log_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    http.paypal_ready()
    http.on("POST", GROQ_URL, (500, {"error": "down"}))
    result = settle(42.50, policy_path, log_path)
    assert result["status"] == "paid"
    assert result["memo_source"].startswith("template (Groq call failed")
    assert "Example Vendor Co" in result["memo"]


def test_draft_payout_memo_requires_a_key(http):
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        settle_deal._draft_payout_memo("V", "tape", 1.0, "USD")


def test_demo_dry_run_end_to_end_makes_no_http_calls(http, capsys, monkeypatch):
    monkeypatch.setattr(demo, "_load_dotenv", lambda path: None)

    assert demo.main([]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN: PAYPAL_CLIENT_ID is not set" in out
    assert "Requests sent to PayPal in total: 0" in out
    for expected in ("ALLOW (local): within policy", "over the per-payment cap", "payee swap",
                     "NEEDS APPROVAL (local)", "approved by demo-approver (simulated)",
                     "already has a payout on record", "over the daily cap",
                     "check failed, payment refused", "verify     OK:", "detected, decision log record"):
        assert expected in out
    assert http.calls == []


def test_demo_live_mode_sends_only_the_allowed_payouts(http, paypal_creds, capsys, monkeypatch):
    monkeypatch.setattr(demo, "_load_dotenv", lambda path: None)
    http.paypal_ready()

    assert demo.main([]) == 0

    out = capsys.readouterr().out
    assert "LIVE SANDBOX" in out and "DRY RUN:" not in out
    amounts = [call["json"]["items"][0]["amount"]["value"] for call in http.to(PAYOUT_URL)]
    assert amounts == ["42.50", "250.00"]
    assert len(http.to(TOKEN_URL)) == 2
    assert "test-client-secret" not in out and "test-access-token" not in out
