"""
Spend approval: the check that runs before any payout leaves this repo.

An agent that can negotiate a price and then pay it is an agent holding a
wallet. This module is the brake. Every intended payment (vendor, recipient,
amount, currency, purpose) is checked against a spending policy and gets one
of three answers:

    allow            the payout may be sent
    deny             the payout must not be sent
    needs_approval   a human has to approve this exact payment first

Rules, in order:

1. Already paid?  A deal that has a recorded payout is never paid twice.
2. Hard rules from the local policy file (spend_policy.json): currency,
   vendor allowlist (the vendor name AND the PayPal email on file must both
   match), per-payment cap, rolling 24-hour cap. These can only deny.
3. Approval rule, from one of two backends:
   - local (default): amounts above `approval_threshold` wait for a human to
     run `python spend_approval.py approve <pending_id>`.
   - Aegis (when AEGIS_AGENT_KEY is set): POST /api/enforce on
     https://aegistrust.digital decides allow / hold / block, and a held
     payment is released with the single-use approval token, sent with the
     identical request, as the Aegis API reference requires.

Fail closed: any error anywhere in the check (unreadable policy, a decision
log that does not verify, Aegis unreachable or answering something
unexpected) is a deny. If AEGIS_AGENT_KEY is set and Aegis cannot be reached,
the answer is deny; the code does not drop back to the weaker local check.

Every decision is appended to a hash-chained JSONL log. Each record carries
the SHA-256 of the previous record, so editing or removing an earlier record
breaks every hash after it. `python spend_approval.py verify` checks the
chain. This is tamper-evident, not tamper-proof: someone who can rewrite the
whole file can rebuild the chain, and trimming the newest records is only
detectable if the head hash was noted somewhere else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

ALLOW = "allow"
DENY = "deny"
NEEDS_APPROVAL = "needs_approval"

DEFAULT_POLICY_FILE = "spend_policy.json"
DEFAULT_LOG_FILE = "spend_decisions.jsonl"
DEFAULT_AEGIS_BASE_URL = "https://aegistrust.digital"
AEGIS_ACTION = "paypal_payout"
WINDOW_SECONDS = 24 * 60 * 60
GENESIS_HASH = "0" * 64

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_DEAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CENT = Decimal("0.01")


class IntentError(ValueError):
    """The intended payment is malformed."""


class PolicyError(ValueError):
    """The policy file is missing or invalid."""


class LogTamperedError(RuntimeError):
    """The decision log does not verify."""


class AegisError(RuntimeError):
    """Aegis could not be reached or answered something unexpected."""


class ApprovalError(RuntimeError):
    """A human approval could not be recorded."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any, name: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise IntentError(f"{name} must be text")
    text = " ".join(value.split())
    if not text:
        raise IntentError(f"{name} is empty")
    if len(text) > max_len:
        raise IntentError(f"{name} is longer than {max_len} characters")
    if _CONTROL_RE.search(text):
        raise IntentError(f"{name} contains control characters")
    return text


def _to_amount(value: Any, name: str = "amount") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise IntentError(f"{name} must be a number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise IntentError(f"{name} is not a number") from exc
    if not amount.is_finite() or amount <= 0:
        raise IntentError(f"{name} must be a positive, finite number")
    rounded = amount.quantize(_CENT)
    if rounded != amount:
        raise IntentError(f"{name} has more than 2 decimal places")
    return rounded


def _vendor_key(vendor: str) -> str:
    return " ".join(vendor.split()).casefold()


def _safe_error(exc: BaseException) -> str:
    """Short description of an error for the log. Our own errors never carry
    key material and httpx errors do not include request headers."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


# --------------------------------------------------------------------------
# the intended payment
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PaymentIntent:
    vendor: str
    recipient_email: str
    amount: Decimal
    currency: str
    purpose: str
    deal_id: str

    @classmethod
    def create(
        cls,
        vendor: Any,
        recipient_email: Any,
        amount: Any,
        currency: Any,
        purpose: Any,
        deal_id: Any = None,
    ) -> "PaymentIntent":
        vendor_text = _clean(vendor, "vendor", 120)
        email = _clean(recipient_email, "recipient_email", 254).lower()
        if not _EMAIL_RE.match(email):
            raise IntentError("recipient_email is not an email address")
        currency_text = _clean(currency, "currency", 3).upper()
        if not _CURRENCY_RE.match(currency_text):
            raise IntentError("currency must be a 3-letter code")
        purpose_text = _clean(purpose, "purpose", 500)
        amount_value = _to_amount(amount)
        if deal_id is None:
            # Same deal terms -> same id, so settling the same deal twice is
            # recognised as a repeat instead of becoming a second payment.
            deal_text = "deal-" + _sha256(_canonical({
                "vendor": vendor_text,
                "recipient_email": email,
                "amount": str(amount_value),
                "currency": currency_text,
                "purpose": purpose_text,
            }))[:16]
        else:
            if not isinstance(deal_id, str) or not _DEAL_ID_RE.match(deal_id):
                raise IntentError("deal_id must be 1-64 characters of A-Z a-z 0-9 . _ : -")
            deal_text = deal_id
        return cls(vendor_text, email, amount_value, currency_text, purpose_text, deal_text)

    def canonical(self) -> dict:
        return {
            "vendor": self.vendor,
            "recipient_email": self.recipient_email,
            "amount": str(self.amount),
            "currency": self.currency,
            "purpose": self.purpose,
            "deal_id": self.deal_id,
        }

    @property
    def intent_hash(self) -> str:
        return _sha256(_canonical(self.canonical()))

    @property
    def sender_batch_id(self) -> str:
        """Deterministic PayPal sender_batch_id for this exact payment, so a
        retry (same process or a later run) can never create a second payout."""
        return "np-" + self.intent_hash[:32]


# --------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SpendPolicy:
    currency: str
    per_payment_cap: Decimal
    daily_cap: Decimal
    approval_threshold: Decimal | None
    approval_ttl_seconds: int
    vendors: dict  # normalised vendor name -> PayPal email on file
    policy_hash: str

    @classmethod
    def from_dict(cls, data: Any) -> "SpendPolicy":
        if not isinstance(data, dict):
            raise PolicyError("policy must be a JSON object")
        try:
            currency = str(data["currency"]).upper()
            per_payment_cap = _to_amount(data["per_payment_cap"], "per_payment_cap")
            daily_cap = _to_amount(data["daily_cap"], "daily_cap")
            raw_threshold = data.get("approval_threshold")
            threshold = None if raw_threshold is None else _to_amount(raw_threshold, "approval_threshold")
            ttl = int(data.get("approval_ttl_seconds", 900))
            entries = data["vendor_allowlist"]
        except KeyError as exc:
            raise PolicyError(f"policy is missing {exc.args[0]!r}") from exc
        except (IntentError, TypeError, ValueError) as exc:
            raise PolicyError(f"policy value is invalid: {exc}") from exc
        if not _CURRENCY_RE.match(currency):
            raise PolicyError("policy currency must be a 3-letter code")
        if not 60 <= ttl <= 86400:
            raise PolicyError("approval_ttl_seconds must be between 60 and 86400")
        if not isinstance(entries, list):
            raise PolicyError("vendor_allowlist must be a list")
        vendors: dict = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise PolicyError("each vendor_allowlist entry must be an object")
            name = entry.get("vendor")
            email = entry.get("paypal_email")
            if not isinstance(name, str) or not name.strip():
                raise PolicyError("vendor_allowlist entry is missing 'vendor'")
            if not isinstance(email, str) or not _EMAIL_RE.match(email.strip()):
                raise PolicyError("vendor_allowlist entry is missing a valid 'paypal_email'")
            vendors[_vendor_key(name)] = email.strip().lower()
        return cls(currency, per_payment_cap, daily_cap, threshold, ttl, vendors, _sha256(_canonical(data)))

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "SpendPolicy":
        policy_path = Path(path or os.environ.get("SPEND_POLICY_FILE") or DEFAULT_POLICY_FILE)
        try:
            data = json.loads(policy_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PolicyError(f"cannot read policy file {policy_path.name}") from exc
        except ValueError as exc:
            raise PolicyError(f"policy file {policy_path.name} is not valid JSON") from exc
        return cls.from_dict(data)

    def hard_rule_violation(self, intent: PaymentIntent, spent_last_24h: Decimal) -> str | None:
        """Return why the payment breaks a hard rule, or None if it does not."""
        if intent.currency != self.currency:
            return f"currency {intent.currency} is not the policy currency {self.currency}"
        on_file = self.vendors.get(_vendor_key(intent.vendor))
        if on_file is None:
            return f"vendor {intent.vendor!r} is not on the allowlist"
        if on_file != intent.recipient_email:
            return (
                f"recipient does not match the PayPal email on file for {intent.vendor!r} "
                "(possible payee swap)"
            )
        if intent.amount > self.per_payment_cap:
            return f"{intent.amount} {intent.currency} is over the per-payment cap {self.per_payment_cap}"
        if spent_last_24h + intent.amount > self.daily_cap:
            return (
                f"{intent.amount} {intent.currency} on top of {spent_last_24h} already allowed "
                f"in the last 24 hours is over the daily cap {self.daily_cap}"
            )
        return None


# --------------------------------------------------------------------------
# the tamper-evident decision log
# --------------------------------------------------------------------------

class DecisionLog:
    """Append-only JSONL file. Each record stores the previous record's hash
    and its own hash over (previous hash + canonical record body)."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.environ.get("SPEND_LOG_PATH") or DEFAULT_LOG_FILE)

    def read(self) -> list:
        """Return every record, verifying the chain. Raises LogTamperedError."""
        if not self.path.exists():
            return []
        records: list = []
        prev = GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise LogTamperedError(f"decision log line {line_no} is not valid JSON") from exc
                if not isinstance(record, dict):
                    raise LogTamperedError(f"decision log line {line_no} is not a record")
                body = {key: value for key, value in record.items() if key != "hash"}
                if (
                    record.get("seq") != line_no
                    or record.get("prev_hash") != prev
                    or record.get("hash") != _sha256(prev + _canonical(body))
                ):
                    raise LogTamperedError(f"decision log record {line_no} does not verify")
                prev = record["hash"]
                records.append(record)
        return records

    def append(self, fields: dict, now: float) -> dict:
        records = self.read()
        prev = records[-1]["hash"] if records else GENESIS_HASH
        body = {
            **fields,
            "seq": len(records) + 1,
            "t": round(float(now), 3),
            "ts": _iso(now),
            "prev_hash": prev,
        }
        record = {**body, "hash": _sha256(prev + _canonical(body))}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def verify(self) -> tuple:
        """(ok, message). The message carries the head hash when ok."""
        try:
            records = self.read()
        except LogTamperedError as exc:
            return False, str(exc)
        head = records[-1]["hash"] if records else GENESIS_HASH
        return True, f"{len(records)} records verify, head {head}"


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    backend: str
    intent: PaymentIntent | None = None
    seq: int | None = None
    record_hash: str | None = None
    pending_id: str | None = None
    expires_at: str | None = None
    failed_closed: bool = False
    already_paid: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "backend": self.backend,
            "intent_hash": self.intent.intent_hash if self.intent else None,
            "seq": self.seq,
            "record_hash": self.record_hash,
            "pending_id": self.pending_id,
            "expires_at": self.expires_at,
            "failed_closed": self.failed_closed,
            "already_paid": self.already_paid,
        }


def _spent_in_window(records: list, now: float, exclude_hash: str) -> Decimal:
    """Total allowed in the last 24 hours, one entry per payment. A payment
    whose payout PayPal definitely refused is released; one whose outcome is
    unknown stays counted."""
    state: dict = {}
    for record in records:
        intent_hash = record.get("intent_hash")
        if record.get("type") == "decision" and record.get("outcome") == ALLOW:
            state[intent_hash] = (Decimal(record["intent"]["amount"]), record["t"], True)
        elif record.get("type") == "payout" and record.get("status") == "failed" and intent_hash in state:
            amount, at, _ = state[intent_hash]
            state[intent_hash] = (amount, at, False)
    cutoff = now - WINDOW_SECONDS
    total = Decimal("0.00")
    for intent_hash, (amount, at, counted) in state.items():
        if counted and at > cutoff and intent_hash != exclude_hash:
            total += amount
    return total


def _open_pending(records: list, intent_hash: str, backend: str) -> dict | None:
    """The record that opened the still-unresolved approval request for this
    payment on this backend, if there is one."""
    current: dict | None = None
    for record in records:
        if record.get("type") != "decision" or record.get("intent_hash") != intent_hash:
            continue
        if record.get("backend") != backend:
            continue
        pending_id = record.get("pending_id")
        if record.get("outcome") == NEEDS_APPROVAL and pending_id:
            if current is None or current["pending_id"] != pending_id:
                current = record
        if current is not None and record.get("resolved_pending_id") == current["pending_id"]:
            current = None
    return current


# --------------------------------------------------------------------------
# Aegis backend (public API only; see the Aegis enforcement API reference)
# --------------------------------------------------------------------------

def _aegis_base_url() -> str:
    base = (os.environ.get("AEGIS_BASE_URL") or DEFAULT_AEGIS_BASE_URL).rstrip("/")
    parsed = urlparse(base)
    local = (parsed.hostname or "") in {"localhost", "127.0.0.1"}
    if parsed.scheme != "https" and not local:
        raise AegisError("AEGIS_BASE_URL must use https")
    return base


def _aegis_scopes() -> list:
    raw = os.environ.get("AEGIS_SCOPES") or "payments"
    scopes = [scope.strip() for scope in raw.split(",") if scope.strip()]
    if not scopes:
        raise AegisError("AEGIS_SCOPES is empty")
    return scopes


def _aegis_request(intent: PaymentIntent) -> dict:
    """The exact action Aegis is asked about. Built only from the payment, so
    it is identical on the first call and on the release call."""
    return {
        "action": AEGIS_ACTION,
        "cost": float(intent.amount),
        "scopes": _aegis_scopes(),
        "params": {
            "amount": str(intent.amount),
            "currency": intent.currency,
            "deal_id": intent.deal_id,
            "purpose": intent.purpose,
            "recipient_email": intent.recipient_email,
            "vendor": intent.vendor,
        },
    }


def _aegis_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise AegisError(f"Aegis answered {response.status_code} without JSON") from exc
    if not isinstance(data, dict):
        raise AegisError(f"Aegis answered {response.status_code} with an unexpected body")
    return data


def _aegis_enforce(base_url: str, agent_key: str, request: dict, approval_token: str | None = None) -> dict:
    """POST /api/enforce. Returns {"outcome", "reason", "released", "pending_id",
    "expires_at", "log_id"}. Raises AegisError on anything not documented."""
    body = dict(request)
    if approval_token is not None:
        # Identical request plus the token, as the API reference specifies.
        body["approvalToken"] = approval_token
    response = httpx.post(
        f"{base_url}/api/enforce",
        content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {agent_key}", "Content-Type": "application/json"},
        timeout=15.0,
    )
    status = response.status_code
    if status not in (200, 202, 403):
        raise AegisError(f"Aegis /api/enforce answered {status}")
    data = _aegis_json(response)
    reason = str(data.get("reason") or data.get("error") or "")[:300]
    log_id = data.get("logId")
    result = {"outcome": DENY, "reason": reason, "released": False, "pending_id": None, "expires_at": None,
              "log_id": str(log_id) if log_id is not None else None}
    if status == 200:
        if data.get("allowed") is not True:
            raise AegisError("Aegis answered 200 without allowed=true")
        result.update(outcome=ALLOW, released=data.get("released") is True, reason=reason or "allowed by Aegis")
    elif status == 202:
        pending_id = data.get("pendingActionId")
        if data.get("requiresApproval") is not True or not isinstance(pending_id, str) or not pending_id:
            raise AegisError("Aegis answered 202 without a pending action id")
        expires_at = data.get("expiresAt")
        result.update(
            outcome=NEEDS_APPROVAL,
            pending_id=pending_id,
            expires_at=expires_at if isinstance(expires_at, str) else None,
            reason=reason or "held for the owner's approval",
        )
    else:
        result.update(reason=reason or "blocked by Aegis")
    return result


def _aegis_get_action(base_url: str, agent_key: str, action_id: str) -> dict:
    """GET /api/actions/:id. Returns {"status", "approval_token"}."""
    response = httpx.get(
        f"{base_url}/api/actions/{quote(action_id, safe='')}",
        headers={"Authorization": f"Bearer {agent_key}"},
        timeout=15.0,
    )
    if response.status_code == 403:
        return {"status": "revoked", "approval_token": None}
    if response.status_code != 200:
        raise AegisError(f"Aegis /api/actions answered {response.status_code}")
    data = _aegis_json(response)
    status = data.get("status")
    if not isinstance(status, str) and isinstance(data.get("action"), dict):
        status = data["action"].get("status")
    if not isinstance(status, str):
        raise AegisError("Aegis /api/actions answered without a status")
    token = data.get("approvalToken")
    return {"status": status, "approval_token": token if isinstance(token, str) and token else None}


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def _run_check(
    intent: PaymentIntent,
    log: DecisionLog,
    policy_path: str | os.PathLike | None,
    now: float,
    dry_run: bool,
    backend_seen: list,
) -> Decision:
    records = log.read()
    intent_hash = intent.intent_hash
    base = {"type": "decision", "intent_hash": intent_hash, "intent": intent.canonical(), "dry_run": dry_run}

    def decide(outcome: str, reason: str, backend: str, **extra: Any) -> Decision:
        # The record is written before the answer is returned: no log, no allow.
        record = log.append({**base, "outcome": outcome, "reason": reason, "backend": backend, **extra}, now)
        return Decision(
            outcome=outcome,
            reason=reason,
            backend=backend,
            intent=intent,
            seq=record["seq"],
            record_hash=record["hash"],
            pending_id=extra.get("pending_id"),
            expires_at=extra.get("expires_at"),
            failed_closed=bool(extra.get("failed_closed")),
            already_paid=bool(extra.get("already_paid")),
        )

    # 1. Never pay the same deal twice.
    for record in records:
        if record.get("type") != "payout" or record.get("intent_hash") != intent_hash:
            continue
        status = record.get("status")
        if status == "sent" or (status == "dry_run" and dry_run):
            batch = record.get("payout_batch_id") or record.get("sender_batch_id")
            return decide(DENY, f"this deal already has a payout on record ({batch})", "local", already_paid=True)

    # 2. Hard rules. These run whichever approval backend is in use.
    policy = SpendPolicy.load(policy_path)
    base["policy_hash"] = policy.policy_hash
    violation = policy.hard_rule_violation(intent, _spent_in_window(records, now, intent_hash))
    if violation:
        return decide(DENY, violation, "local")

    # 3. Approval rule.
    agent_key = os.environ.get("AEGIS_AGENT_KEY")
    if agent_key:
        backend_seen[0] = "aegis"
        return _check_with_aegis(intent, records, decide, agent_key)
    return _check_locally(intent, records, decide, policy, now)


def _check_locally(intent: PaymentIntent, records: list, decide: Any, policy: SpendPolicy, now: float) -> Decision:
    threshold = policy.approval_threshold
    if threshold is None or intent.amount <= threshold:
        return decide(ALLOW, "within policy", "local")

    pending = _open_pending(records, intent.intent_hash, "local")
    if pending is None:
        pending_id = "apr-" + _sha256(intent.intent_hash + repr(now))[:12]
        expires_t = now + policy.approval_ttl_seconds
        return decide(
            NEEDS_APPROVAL,
            f"{intent.amount} {intent.currency} is above the approval threshold {threshold}; a human must approve",
            "local",
            pending_id=pending_id,
            expires_at=_iso(expires_t),
            expires_t=expires_t,
        )

    pending_id = pending["pending_id"]
    human = next(
        (r for r in records if r.get("type") == "approval" and r.get("pending_id") == pending_id),
        None,
    )
    if human is None:
        if now > pending["expires_t"]:
            return decide(DENY, "approval request expired before a human decided", "local",
                          resolved_pending_id=pending_id)
        return decide(
            NEEDS_APPROVAL,
            "still waiting for a human to approve",
            "local",
            pending_id=pending_id,
            expires_at=pending["expires_at"],
            expires_t=pending["expires_t"],
        )
    approver = human.get("approver") or "unknown"
    if human.get("approved") is not True:
        return decide(DENY, f"denied by {approver}", "local", resolved_pending_id=pending_id)
    if now - human["t"] > policy.approval_ttl_seconds:
        return decide(DENY, "approval expired before it was used", "local", resolved_pending_id=pending_id)
    # One approval releases one payment: this record marks it as used.
    return decide(ALLOW, f"approved by {approver}", "local", resolved_pending_id=pending_id, approved_by=approver)


def _check_with_aegis(intent: PaymentIntent, records: list, decide: Any, agent_key: str) -> Decision:
    if not agent_key.startswith("aeg_agt_"):
        raise AegisError("AEGIS_AGENT_KEY is not an agent key (expected the aeg_agt_ prefix)")
    if intent.currency != "USD":
        return decide(DENY, "Aegis costs are in US dollars; non-USD payouts are refused", "aegis")
    base_url = _aegis_base_url()
    request = _aegis_request(intent)

    pending = _open_pending(records, intent.intent_hash, "aegis")
    if pending is None:
        result = _aegis_enforce(base_url, agent_key, request)
        extra: dict = {"aegis_log_id": result["log_id"]}
        if result["outcome"] == NEEDS_APPROVAL:
            extra.update(pending_id=result["pending_id"], expires_at=result["expires_at"])
        return decide(result["outcome"], result["reason"], "aegis", **extra)

    pending_id = pending["pending_id"]
    action = _aegis_get_action(base_url, agent_key, pending_id)
    status = action["status"]
    if status == "pending":
        return decide(NEEDS_APPROVAL, "still waiting for the owner's approval in Aegis", "aegis",
                      pending_id=pending_id, expires_at=pending.get("expires_at"))
    if status != "approved":
        if status not in {"denied", "expired", "cancelled", "consumed", "revoked"}:
            raise AegisError("Aegis reported an unknown action status")
        return decide(DENY, f"Aegis approval request is {status}", "aegis", resolved_pending_id=pending_id)

    token = action["approval_token"]
    if token is None:
        # Aegis hands the token out once. Without it this approval cannot be used.
        return decide(DENY, "approved, but the single-use approval token was already collected", "aegis",
                      resolved_pending_id=pending_id)
    try:
        result = _aegis_enforce(base_url, agent_key, request, approval_token=token)
    except Exception as exc:
        # The token may or may not have been consumed. Either way: no payout.
        return decide(DENY, f"release failed, payment refused: {_safe_error(exc)}", "aegis",
                      resolved_pending_id=pending_id, failed_closed=True)
    if result["outcome"] == ALLOW and result["released"]:
        return decide(ALLOW, result["reason"] or "released by owner approval", "aegis",
                      resolved_pending_id=pending_id, aegis_log_id=result["log_id"])
    if result["outcome"] == ALLOW:
        return decide(DENY, "Aegis did not confirm the approval token was consumed", "aegis",
                      resolved_pending_id=pending_id, failed_closed=True)
    return decide(DENY, result["reason"] or "release refused by Aegis", "aegis",
                  resolved_pending_id=pending_id, aegis_log_id=result["log_id"])


def authorize_payment(
    vendor: Any,
    recipient_email: Any,
    amount: Any,
    currency: Any,
    purpose: Any,
    *,
    deal_id: str | None = None,
    policy_path: str | os.PathLike | None = None,
    log_path: str | os.PathLike | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> Decision:
    """Check one intended payment. Returns a Decision; never raises.

    Only `decision.allowed` (outcome == "allow") permits a payout. Every other
    path, including any exception inside the check, is a refusal.
    """
    moment = time.time() if now is None else float(now)
    backend_seen = ["local"]
    intent: PaymentIntent | None = None
    log: DecisionLog | None = None
    try:
        log = DecisionLog(log_path)
        intent = PaymentIntent.create(vendor, recipient_email, amount, currency, purpose, deal_id)
        return _run_check(intent, log, policy_path, moment, dry_run, backend_seen)
    except Exception as exc:  # fail closed, whatever went wrong
        reason = f"check failed, payment refused: {_safe_error(exc)}"
        seq = record_hash = None
        try:
            if log is not None:
                record = log.append(
                    {
                        "type": "decision",
                        "intent_hash": intent.intent_hash if intent else None,
                        "intent": intent.canonical() if intent else None,
                        "dry_run": dry_run,
                        "outcome": DENY,
                        "reason": reason,
                        "backend": backend_seen[0],
                        "failed_closed": True,
                    },
                    moment,
                )
                seq, record_hash = record["seq"], record["hash"]
        except Exception:
            pass  # the log itself is unusable; the answer is still deny
        return Decision(DENY, reason, backend_seen[0], intent=intent, seq=seq, record_hash=record_hash,
                        failed_closed=True)


def record_payout(
    intent: PaymentIntent,
    status: str,
    *,
    payout_batch_id: str | None = None,
    detail: str | None = None,
    log_path: str | os.PathLike | None = None,
    now: float | None = None,
) -> dict:
    """Append what happened at PayPal. status is one of:
    sent (PayPal accepted the batch), dry_run (nothing was sent),
    failed (definitely not sent), unknown (no definite answer from PayPal)."""
    if status not in {"sent", "dry_run", "failed", "unknown"}:
        raise ValueError("unknown payout status")
    return DecisionLog(log_path).append(
        {
            "type": "payout",
            "intent_hash": intent.intent_hash,
            "intent": intent.canonical(),
            "status": status,
            "sender_batch_id": intent.sender_batch_id,
            "payout_batch_id": payout_batch_id,
            "detail": detail[:300] if detail else None,
        },
        time.time() if now is None else float(now),
    )


# --------------------------------------------------------------------------
# the human side of the local backend
# --------------------------------------------------------------------------

def list_pending(*, log_path: str | os.PathLike | None = None, now: float | None = None) -> list:
    """Open local approval requests that a human can still decide."""
    moment = time.time() if now is None else float(now)
    records = DecisionLog(log_path).read()
    decided = {r.get("pending_id") for r in records if r.get("type") == "approval"}
    resolved = {r.get("resolved_pending_id") for r in records if r.get("type") == "decision"}
    seen: set = set()
    pending = []
    for record in records:
        pending_id = record.get("pending_id")
        if (
            record.get("type") == "decision"
            and record.get("backend") == "local"
            and record.get("outcome") == NEEDS_APPROVAL
            and pending_id not in seen
        ):
            seen.add(pending_id)
            if pending_id not in decided and pending_id not in resolved and moment <= record["expires_t"]:
                pending.append(record)
    return pending


def record_human_decision(
    pending_id: str,
    approve: bool,
    approver: str,
    *,
    log_path: str | os.PathLike | None = None,
    now: float | None = None,
) -> dict:
    """A human approves or denies one pending payment (local backend).

    Anyone who can write the log file can run this. That is a separate step,
    not a separate privilege; with the Aegis backend the approval needs the
    owner key, which the agent does not hold.
    """
    moment = time.time() if now is None else float(now)
    name = _clean(approver, "approver", 80)
    log = DecisionLog(log_path)
    target = next((r for r in list_pending(log_path=log.path, now=moment) if r.get("pending_id") == pending_id), None)
    if target is None:
        raise ApprovalError("no open approval request with that id (unknown, already decided, or expired)")
    return log.append(
        {
            "type": "approval",
            "pending_id": pending_id,
            "intent_hash": target["intent_hash"],
            "intent": target["intent"],
            "approved": bool(approve),
            "approver": name,
        },
        moment,
    )


# --------------------------------------------------------------------------
# CLI for the human: python spend_approval.py pending | approve | deny | verify | show
# --------------------------------------------------------------------------

def _cli(argv: list | None = None) -> int:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description="Human side of the spend-approval log.")
    parser.add_argument("--log", default=None, help="decision log path (default: SPEND_LOG_PATH or spend_decisions.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending", help="list payments waiting for approval")
    for name in ("approve", "deny"):
        cmd = sub.add_parser(name, help=f"{name} one pending payment")
        cmd.add_argument("pending_id")
        cmd.add_argument("--by", default=None, help="your name, recorded in the log")
    sub.add_parser("verify", help="check the hash chain of the decision log")
    sub.add_parser("show", help="print the decision log")
    args = parser.parse_args(argv)

    log = DecisionLog(args.log)
    try:
        if args.command == "verify":
            ok, message = log.verify()
            print(("OK: " if ok else "TAMPERED: ") + message)
            return 0 if ok else 1
        if args.command == "pending":
            pending = list_pending(log_path=log.path)
            if not pending:
                print("No payments are waiting for approval.")
            for record in pending:
                intent = record["intent"]
                print(f"{record['pending_id']}  {intent['amount']} {intent['currency']} to {intent['vendor']} "
                      f"<{intent['recipient_email']}>  for: {intent['purpose']}  (expires {record['expires_at']})")
            return 0
        if args.command == "show":
            for record in log.read():
                kind = record.get("type")
                intent = record.get("intent") or {}
                what = record.get("outcome") or record.get("status") or ("approved" if record.get("approved") else "denied")
                print(f"{record['seq']:>3}  {record['ts']}  {kind:<8}  {what:<14}  "
                      f"{intent.get('amount', '-')} {intent.get('currency', '')}  {intent.get('vendor', '-')}  "
                      f"{record.get('reason') or record.get('detail') or ''}")
            return 0
        approver = args.by or getpass.getuser()
        record = record_human_decision(args.pending_id, args.command == "approve", approver, log_path=log.path)
        print(f"{'Approved' if record['approved'] else 'Denied'} {args.pending_id} as {approver}. "
              f"Log head is now {record['hash']}")
        return 0
    except (ApprovalError, LogTamperedError, IntentError) as exc:
        print(f"Refused: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
