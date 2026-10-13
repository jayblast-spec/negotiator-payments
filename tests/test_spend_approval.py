"""spend_approval: policy paths, human approval, the log, and failing closed."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import spend_approval as sa
from conftest import AEGIS, ENFORCE_URL, POLICY

T0 = 1_800_000_000.0
VENDOR = "Example Vendor Co"
EMAIL = "vendor@example.com"


def check(amount, policy_path, log_path, *, now=T0, vendor=VENDOR, email=EMAIL, currency="USD",
          purpose="packaging tape", deal_id=None, dry_run=False):
    return sa.authorize_payment(vendor, email, amount, currency, purpose, deal_id=deal_id,
                                policy_path=policy_path, log_path=log_path, now=now, dry_run=dry_run)


# ---------------------------------------------------------------- local policy

def test_allow_within_policy(http, policy_path, log_path):
    decision = check(42.50, policy_path, log_path)
    assert decision.allowed and decision.outcome == sa.ALLOW
    assert decision.backend == "local"
    assert decision.record_hash and decision.seq == 1
    assert http.calls == []


def test_deny_over_per_payment_cap(policy_path, log_path):
    decision = check(500.01, policy_path, log_path)
    assert decision.outcome == sa.DENY and not decision.allowed
    assert "per-payment cap" in decision.reason


def test_amount_exactly_at_cap_is_not_denied_by_the_cap(policy_path, log_path):
    assert check(500, policy_path, log_path).outcome == sa.NEEDS_APPROVAL


def test_deny_vendor_not_on_allowlist(policy_path, log_path):
    decision = check(20, policy_path, log_path, vendor="Totally New Vendor LLC")
    assert decision.outcome == sa.DENY
    assert "not on the allowlist" in decision.reason


def test_deny_when_recipient_email_differs_from_the_one_on_file(policy_path, log_path):
    decision = check(20, policy_path, log_path, email="payments@examp1e-vendor.example")
    assert decision.outcome == sa.DENY
    assert "payee swap" in decision.reason


def test_vendor_match_ignores_case_and_spacing(policy_path, log_path):
    assert check(20, policy_path, log_path, vendor="  example   VENDOR co ", email="Vendor@Example.com").allowed


def test_deny_wrong_currency(policy_path, log_path):
    decision = check(20, policy_path, log_path, currency="EUR")
    assert decision.outcome == sa.DENY and "currency" in decision.reason


def test_deny_over_daily_cap_and_window_rolls(policy_path, log_path):
    for index in range(6):
        assert check(95, policy_path, log_path, deal_id=f"d{index}", now=T0 + index).allowed  # 570.00
    over = check(95, policy_path, log_path, deal_id="d6", now=T0 + 10)
    assert over.outcome == sa.DENY and "daily cap" in over.reason
    # 24 hours after the first payments, the window has room again.
    assert check(95, policy_path, log_path, deal_id="d7", now=T0 + sa.WINDOW_SECONDS + 3).allowed


def test_payout_that_definitely_failed_is_released_from_the_daily_total(policy_path, log_path):
    first = check(95, policy_path, log_path, deal_id="a", now=T0)
    for index in range(5):
        check(95, policy_path, log_path, deal_id=f"b{index}", now=T0 + 1)
    assert check(95, policy_path, log_path, deal_id="c", now=T0 + 2).outcome == sa.DENY
    sa.record_payout(first.intent, "failed", log_path=log_path, now=T0 + 3)
    assert check(95, policy_path, log_path, deal_id="c", now=T0 + 4).allowed


def test_unknown_payout_outcome_stays_counted(policy_path, log_path):
    first = check(95, policy_path, log_path, deal_id="a", now=T0)
    for index in range(5):
        check(95, policy_path, log_path, deal_id=f"b{index}", now=T0 + 1)
    sa.record_payout(first.intent, "unknown", log_path=log_path, now=T0 + 3)
    assert check(95, policy_path, log_path, deal_id="c", now=T0 + 4).outcome == sa.DENY


# ---------------------------------------------------------------- human approval

def test_needs_approval_then_human_approves_then_allow_once(policy_path, log_path):
    held = check(250, policy_path, log_path)
    assert held.outcome == sa.NEEDS_APPROVAL and not held.allowed
    assert held.pending_id and held.expires_at

    waiting = check(250, policy_path, log_path, now=T0 + 5)
    assert waiting.outcome == sa.NEEDS_APPROVAL
    assert waiting.pending_id == held.pending_id

    assert [r["pending_id"] for r in sa.list_pending(log_path=log_path, now=T0 + 6)] == [held.pending_id]
    sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 10)
    assert sa.list_pending(log_path=log_path, now=T0 + 11) == []

    released = check(250, policy_path, log_path, now=T0 + 20)
    assert released.allowed
    assert "approved by Dana" in released.reason

    # The approval was for one payment. Asking again starts over.
    again = check(250, policy_path, log_path, now=T0 + 30)
    assert again.outcome == sa.NEEDS_APPROVAL
    assert again.pending_id != held.pending_id


def test_approval_does_not_cover_a_different_amount_or_recipient(policy_path, log_path):
    held = check(250, policy_path, log_path)
    sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 10)
    assert check(450, policy_path, log_path, now=T0 + 20).outcome == sa.NEEDS_APPROVAL
    assert check(250, policy_path, log_path, now=T0 + 20, vendor="Northside Packaging",
                 email="ap@northside-packaging.example").outcome == sa.NEEDS_APPROVAL


def test_human_denial_is_a_deny(policy_path, log_path):
    held = check(250, policy_path, log_path)
    sa.record_human_decision(held.pending_id, False, "Dana", log_path=log_path, now=T0 + 10)
    decision = check(250, policy_path, log_path, now=T0 + 20)
    assert decision.outcome == sa.DENY and "denied by Dana" in decision.reason


def test_request_expires_if_nobody_decides(policy_path, log_path):
    held = check(250, policy_path, log_path)
    late = check(250, policy_path, log_path, now=T0 + 901)
    assert late.outcome == sa.DENY and "expired" in late.reason
    with pytest.raises(sa.ApprovalError):
        sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 902)


def test_approval_expires_if_not_used_in_time(policy_path, log_path):
    held = check(250, policy_path, log_path)
    sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 10)
    late = check(250, policy_path, log_path, now=T0 + 10 + 901)
    assert late.outcome == sa.DENY and "expired" in late.reason


def test_cannot_decide_twice_or_decide_an_unknown_request(policy_path, log_path):
    held = check(250, policy_path, log_path)
    sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 1)
    with pytest.raises(sa.ApprovalError):
        sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 2)
    with pytest.raises(sa.ApprovalError):
        sa.record_human_decision("apr-doesnotexist", True, "Dana", log_path=log_path, now=T0 + 2)


def test_approved_payment_still_has_to_pass_the_hard_rules(policy_path, log_path, tmp_path):
    held = check(250, policy_path, log_path)
    sa.record_human_decision(held.pending_id, True, "Dana", log_path=log_path, now=T0 + 10)
    tighter = dict(POLICY, per_payment_cap="200.00")
    tight_path = tmp_path / "tight.json"
    tight_path.write_text(json.dumps(tighter), encoding="utf-8")
    decision = check(250, str(tight_path), log_path, now=T0 + 20)
    assert decision.outcome == sa.DENY and "per-payment cap" in decision.reason


# ---------------------------------------------------------------- fail closed

def test_fail_closed_when_policy_file_is_missing(tmp_path, log_path):
    decision = check(20, str(tmp_path / "nope.json"), log_path)
    assert decision.outcome == sa.DENY and decision.failed_closed and not decision.allowed
    assert "check failed" in decision.reason
    assert sa.DecisionLog(log_path).read()[0]["failed_closed"] is True


@pytest.mark.parametrize("broken", [
    "{not json",
    json.dumps({"currency": "USD"}),
    json.dumps(dict(POLICY, per_payment_cap="-5")),
    json.dumps(dict(POLICY, vendor_allowlist="everyone")),
    json.dumps([1, 2, 3]),
])
def test_fail_closed_when_policy_file_is_invalid(tmp_path, log_path, broken):
    path = tmp_path / "bad.json"
    path.write_text(broken, encoding="utf-8")
    decision = check(20, str(path), log_path)
    assert decision.outcome == sa.DENY and decision.failed_closed


@pytest.mark.parametrize("amount", [0, -5, "abc", float("nan"), float("inf"), 10.005, None, True])
def test_fail_closed_on_a_malformed_amount(policy_path, log_path, amount):
    decision = check(amount, policy_path, log_path)
    assert decision.outcome == sa.DENY and decision.failed_closed


def test_fail_closed_on_a_malformed_recipient(policy_path, log_path):
    assert check(20, policy_path, log_path, email="not-an-email").failed_closed


def test_fail_closed_when_the_log_has_been_tampered_with(policy_path, log_path):
    assert check(20, policy_path, log_path, deal_id="a").allowed
    assert check(30, policy_path, log_path, deal_id="b").allowed
    path = Path(log_path)
    path.write_text(path.read_text(encoding="utf-8").replace('"amount":"20.00"', '"amount":"2.00"'), encoding="utf-8")

    decision = check(40, policy_path, log_path, deal_id="c")

    assert decision.outcome == sa.DENY and decision.failed_closed
    assert "LogTamperedError" in decision.reason
    assert decision.record_hash is None  # nothing is appended to a log that does not verify


def test_fail_closed_when_the_log_cannot_be_written(policy_path, tmp_path):
    # The log path is a directory, so appending must fail.
    decision = check(20, policy_path, str(tmp_path))
    assert decision.outcome == sa.DENY and decision.failed_closed


def test_fail_closed_on_an_unexpected_error_inside_the_check(policy_path, log_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ZeroDivisionError("bug")

    monkeypatch.setattr(sa.SpendPolicy, "hard_rule_violation", boom)
    decision = check(20, policy_path, log_path)
    assert decision.outcome == sa.DENY and decision.failed_closed


# ---------------------------------------------------------------- the log

def test_log_is_hash_chained_and_verifies(policy_path, log_path):
    check(20, policy_path, log_path, deal_id="a")
    check(900, policy_path, log_path, deal_id="b")
    records = sa.DecisionLog(log_path).read()
    assert [r["seq"] for r in records] == [1, 2]
    assert records[0]["prev_hash"] == sa.GENESIS_HASH
    assert records[1]["prev_hash"] == records[0]["hash"]
    ok, message = sa.DecisionLog(log_path).verify()
    assert ok and records[1]["hash"] in message


@pytest.mark.parametrize("edit", [
    lambda lines: [lines[0].replace('"outcome":"deny"', '"outcome":"allow"')] + lines[1:],  # rewrite a decision
    lambda lines: lines[1:],                                                               # drop the first record
    lambda lines: [lines[1], lines[0]] + lines[2:],                                        # reorder
    lambda lines: lines[:1] + ["{}"] + lines[1:],                                          # insert a record
])
def test_log_tampering_is_detected(policy_path, log_path, edit):
    check(900, policy_path, log_path, deal_id="a")
    check(20, policy_path, log_path, deal_id="b")
    check(30, policy_path, log_path, deal_id="c")
    path = Path(log_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(edit(lines)) + "\n", encoding="utf-8")
    ok, message = sa.DecisionLog(log_path).verify()
    assert not ok and "does not verify" in message


def test_already_paid_deal_is_refused(policy_path, log_path):
    first = check(20, policy_path, log_path)
    sa.record_payout(first.intent, "sent", payout_batch_id="BATCH1", log_path=log_path, now=T0 + 1)
    again = check(20, policy_path, log_path, now=T0 + 2)
    assert again.outcome == sa.DENY and again.already_paid
    assert "BATCH1" in again.reason


def test_same_terms_give_the_same_sender_batch_id_and_different_terms_do_not():
    a = sa.PaymentIntent.create(VENDOR, EMAIL, 250.0, "usd", "tape")
    b = sa.PaymentIntent.create(VENDOR, EMAIL, "250.00", "USD", "tape")
    c = sa.PaymentIntent.create(VENDOR, EMAIL, 250.01, "USD", "tape")
    d = sa.PaymentIntent.create(VENDOR, EMAIL, 250.0, "USD", "tape", deal_id="second-order")
    assert a.sender_batch_id == b.sender_batch_id
    assert len({a.sender_batch_id, c.sender_batch_id, d.sender_batch_id}) == 3
    assert a.sender_batch_id.startswith("np-") and len(a.sender_batch_id) == 35


# ---------------------------------------------------------------- Aegis backend

AGENT_KEY = "aeg_agt_test_key_not_real"
ACTION_URL = f"{AEGIS}/api/actions/act-123"


@pytest.fixture
def aegis(monkeypatch):
    monkeypatch.setenv("AEGIS_AGENT_KEY", AGENT_KEY)


def test_aegis_allow(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL, (200, {"allowed": True, "requiresApproval": False, "reason": "Within limits.", "logId": "41"}))

    decision = check(42.50, policy_path, log_path, deal_id="d1")

    assert decision.allowed and decision.backend == "aegis"
    (call,) = http.calls
    assert call["url"] == ENFORCE_URL and call["method"] == "POST"
    assert call["headers"]["Authorization"] == f"Bearer {AGENT_KEY}"
    assert call["json"] == {
        "action": "paypal_payout",
        "cost": 42.5,
        "scopes": ["payments"],
        "params": {
            "amount": "42.50", "currency": "USD", "deal_id": "d1", "purpose": "packaging tape",
            "recipient_email": EMAIL, "vendor": VENDOR,
        },
    }
    assert sa.DecisionLog(log_path).read()[0]["aegis_log_id"] == "41"


def test_aegis_block_is_a_deny(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL, (403, {"allowed": False, "reason": "Cost $42.50 exceeds per-action cap $10.00."}))
    decision = check(42.50, policy_path, log_path)
    assert decision.outcome == sa.DENY and not decision.failed_closed
    assert "per-action cap" in decision.reason


def test_local_hard_rules_run_before_aegis_is_asked(http, aegis, policy_path, log_path):
    decision = check(2400, policy_path, log_path)
    assert decision.outcome == sa.DENY and decision.backend == "local"
    assert http.calls == []


def test_aegis_hold_then_release_sends_the_identical_request_plus_token(http, aegis, policy_path, log_path):
    token = "aeg_apv_single_use_token"
    http.on("POST", ENFORCE_URL,
            (202, {"allowed": False, "requiresApproval": True, "reason": "Human approval required.",
                   "pendingActionId": "act-123", "expiresAt": "2027-01-15T08:15:00.000Z", "logId": "42"}),
            (200, {"allowed": True, "released": True, "reason": "Released.", "logId": "44"}))
    http.on("GET", ACTION_URL, (200, {"status": "pending"}), (200, {"status": "approved", "approvalToken": token}))

    held = check(250, policy_path, log_path, deal_id="d1")
    assert held.outcome == sa.NEEDS_APPROVAL and held.pending_id == "act-123"
    assert held.expires_at == "2027-01-15T08:15:00.000Z"

    waiting = check(250, policy_path, log_path, deal_id="d1", now=T0 + 5)
    assert waiting.outcome == sa.NEEDS_APPROVAL
    assert len(http.to(ENFORCE_URL)) == 1  # polling does not re-ask /api/enforce

    released = check(250, policy_path, log_path, deal_id="d1", now=T0 + 10)
    assert released.allowed

    first, second = http.to(ENFORCE_URL)
    assert "approvalToken" not in first["json"]
    assert second["json"] == {**first["json"], "approvalToken": token}
    # Byte for byte: the original body with the token appended as the last key.
    assert second["raw"] == first["raw"][:-1] + f',"approvalToken":"{token}"}}'.encode()
    assert second["headers"]["Authorization"] == f"Bearer {AGENT_KEY}"
    polls = http.to(ACTION_URL)
    assert len(polls) == 2 and polls[0]["headers"]["Authorization"] == f"Bearer {AGENT_KEY}"
    # Neither the approval token nor the agent key is ever written to the log.
    log_text = Path(log_path).read_text(encoding="utf-8")
    assert token not in log_text and AGENT_KEY not in log_text


def test_aegis_replayed_token_is_a_deny(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL,
            (202, {"requiresApproval": True, "pendingActionId": "act-123", "expiresAt": "2027-01-15T08:15:00.000Z"}),
            (403, {"allowed": False, "reason": "Approval token already used."}))
    http.on("GET", ACTION_URL, (200, {"status": "approved", "approvalToken": "aeg_apv_used"}))
    check(250, policy_path, log_path, deal_id="d1")
    decision = check(250, policy_path, log_path, deal_id="d1", now=T0 + 5)
    assert decision.outcome == sa.DENY and "already used" in decision.reason


def test_aegis_release_without_released_flag_is_refused(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL,
            (202, {"requiresApproval": True, "pendingActionId": "act-123"}),
            (200, {"allowed": True}))
    http.on("GET", ACTION_URL, (200, {"status": "approved", "approvalToken": "aeg_apv_x"}))
    check(250, policy_path, log_path, deal_id="d1")
    decision = check(250, policy_path, log_path, deal_id="d1", now=T0 + 5)
    assert decision.outcome == sa.DENY and decision.failed_closed


def test_aegis_token_already_collected_is_a_deny(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL, (202, {"requiresApproval": True, "pendingActionId": "act-123"}))
    http.on("GET", ACTION_URL, (200, {"status": "approved", "tokenIssued": True}))
    check(250, policy_path, log_path, deal_id="d1")
    decision = check(250, policy_path, log_path, deal_id="d1", now=T0 + 5)
    assert decision.outcome == sa.DENY and "already collected" in decision.reason
    assert len(http.to(ENFORCE_URL)) == 1


@pytest.mark.parametrize("status", ["denied", "expired", "cancelled", "consumed"])
def test_aegis_closed_request_is_a_deny(http, aegis, policy_path, log_path, status):
    http.on("POST", ENFORCE_URL, (202, {"requiresApproval": True, "pendingActionId": "act-123"}))
    http.on("GET", ACTION_URL, (200, {"status": status}))
    check(250, policy_path, log_path, deal_id="d1")
    decision = check(250, policy_path, log_path, deal_id="d1", now=T0 + 5)
    assert decision.outcome == sa.DENY and status in decision.reason


def test_aegis_revoked_agent_is_a_deny(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL, (202, {"requiresApproval": True, "pendingActionId": "act-123"}))
    http.on("GET", ACTION_URL, (403, {"error": "Agent revoked", "code": "forbidden"}))
    check(250, policy_path, log_path, deal_id="d1")
    assert check(250, policy_path, log_path, deal_id="d1", now=T0 + 5).outcome == sa.DENY


@pytest.mark.parametrize("reply", [
    (500, {"error": "boom", "code": "internal"}),
    (401, {"error": "Agent key required", "code": "unauthorized"}),
    (429, {"error": "rate limited", "code": "rate_limited"}),
    (200, {"allowed": False}),
    (200, {"something": "else"}),
    (202, {"requiresApproval": True}),
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("timed out"),
])
def test_fail_closed_when_aegis_errors_and_no_fallback_to_local(http, aegis, policy_path, log_path, reply):
    http.on("POST", ENFORCE_URL, reply)
    # 42.50 would be a plain allow under the local policy. With an Aegis key
    # configured, an Aegis failure must not quietly use that weaker answer.
    decision = check(42.50, policy_path, log_path)
    assert decision.outcome == sa.DENY and decision.failed_closed and not decision.allowed
    assert decision.backend == "aegis"


def test_fail_closed_when_the_aegis_poll_errors_and_the_request_stays_open(http, aegis, policy_path, log_path):
    http.on("POST", ENFORCE_URL, (202, {"requiresApproval": True, "pendingActionId": "act-123"}))
    http.on("GET", ACTION_URL, httpx.ConnectError("down"), (200, {"status": "pending"}))
    check(250, policy_path, log_path, deal_id="d1")
    assert check(250, policy_path, log_path, deal_id="d1", now=T0 + 5).failed_closed
    # The next run polls the same request again rather than opening a new one.
    assert check(250, policy_path, log_path, deal_id="d1", now=T0 + 10).outcome == sa.NEEDS_APPROVAL
    assert len(http.to(ENFORCE_URL)) == 1


def test_fail_closed_when_the_key_is_not_an_agent_key(http, policy_path, log_path, monkeypatch):
    monkeypatch.setenv("AEGIS_AGENT_KEY", "aeg_own_owner_key_pasted_by_mistake")
    decision = check(42.50, policy_path, log_path)
    assert decision.outcome == sa.DENY and decision.failed_closed
    assert http.calls == []
    assert "aeg_own_owner_key_pasted_by_mistake" not in Path(log_path).read_text(encoding="utf-8")


def test_fail_closed_when_aegis_url_is_not_https(http, aegis, policy_path, log_path, monkeypatch):
    monkeypatch.setenv("AEGIS_BASE_URL", "http://aegistrust.digital")
    assert check(42.50, policy_path, log_path).failed_closed
    assert http.calls == []
