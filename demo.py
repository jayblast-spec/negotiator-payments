"""
End-to-end demo: negotiated deals go through the spend-approval check and,
only when allowed, out through PayPal Payouts.

    python demo.py                    # dry run unless PayPal credentials are set
    python demo.py --dry-run          # force a dry run even with credentials
    python demo.py --manual-approval  # you approve the held payment yourself

With PAYPAL_CLIENT_ID unset this is a DRY RUN: every decision is real, the
exact PayPal request body is built and shown, and nothing is sent to PayPal.
With PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET set, allowed payments are sent
to the PayPal sandbox as real sandbox payouts.

The demo keeps its policy and decision log in a fresh temporary folder, so it
never touches spend_decisions.jsonl in the repo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

import spend_approval
from settle_deal import settle_negotiated_deal

REPO = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


class _PayPalCallCounter:
    """Counts requests that go to the PayPal API host. Records nothing else."""

    def __init__(self) -> None:
        self.count = 0
        self._real_post = httpx.post
        self._host = urlparse(os.environ.get("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")).hostname

    def install(self) -> None:
        def counted_post(url, *args, **kwargs):
            if urlparse(str(url)).hostname == self._host:
                self.count += 1
            return self._real_post(url, *args, **kwargs)

        httpx.post = counted_post

    def uninstall(self) -> None:
        httpx.post = self._real_post


def _say(label: str, text: str) -> None:
    print(f"  {label:<10} {text}")


def _show(result: dict, calls_before: int, counter: _PayPalCallCounter) -> None:
    decision = result["decision"]
    outcome = decision["outcome"].upper().replace("_", " ")
    _say("decision", f"{outcome} ({decision['backend']}): {decision['reason']}")
    status = result["status"]
    if status == "paid":
        header = (result.get("payout") or {}).get("batch_header") or {}
        _say("memo", f"{result['memo']}  [{result['memo_source']}]")
        _say("paypal", f"payout sent. batch {header.get('payout_batch_id')} status {header.get('batch_status')}")
    elif status == "dry_run":
        _say("memo", f"{result['memo']}  [{result['memo_source']}]")
        _say("paypal", "DRY RUN, not sent. Request that would go to POST /v1/payments/payouts:")
        print("             " + json.dumps(result["payout_request"]))
    elif status in ("payout_failed", "payout_unknown"):
        _say("paypal", f"{status}: {result.get('error')}")
    else:
        _say("paypal", "not called")
    _say("calls", f"requests sent to PayPal for this step: {counter.count - calls_before}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spend-approved PayPal payouts, end to end.")
    parser.add_argument("--dry-run", action="store_true", help="never send anything to PayPal")
    parser.add_argument("--manual-approval", action="store_true",
                        help="wait for you to approve the held payment in a second terminal")
    parser.add_argument("--approval-wait", type=int, default=180, help="seconds to wait for a human approval")
    args = parser.parse_args(argv)

    _load_dotenv(REPO / ".env")
    live = bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")) and not args.dry_run
    dry_run = not live
    aegis = bool(os.environ.get("AEGIS_AGENT_KEY"))

    vendor = os.environ.get("DEMO_VENDOR", "Example Vendor Co")
    vendor_email = os.environ.get("DEMO_VENDOR_PAYPAL_EMAIL", "vendor@example.com")

    workdir = Path(tempfile.mkdtemp(prefix="negotiator-payments-demo-"))
    log_path = str(workdir / "spend_decisions.jsonl")
    policy = json.loads((REPO / "spend_policy.json").read_text(encoding="utf-8"))
    policy["vendor_allowlist"] = [{"vendor": vendor, "paypal_email": vendor_email}]
    policy_path = str(workdir / "spend_policy.json")
    Path(policy_path).write_text(json.dumps(policy, indent=2), encoding="utf-8")
    run_id = format(int(time.time()), "x")

    counter = _PayPalCallCounter()
    counter.install()

    def settle(step: str, price: float, purchase: str, email: str = vendor_email, policy_file: str = policy_path) -> dict:
        return settle_negotiated_deal(
            vendor, email, purchase, price,
            deal_id=f"demo-{run_id}-{step}", dry_run=dry_run, policy_path=policy_file, log_path=log_path,
        )

    print("=" * 78)
    if dry_run:
        why = "--dry-run was passed" if args.dry_run else "PAYPAL_CLIENT_ID is not set"
        print(f"DRY RUN: {why}. Nothing is sent to PayPal.")
        print("Decisions, the approval step and the decision log are real; payouts are not.")
    else:
        print("LIVE SANDBOX: allowed payments are sent to the PayPal sandbox as real sandbox payouts.")
    print("=" * 78)
    print(f"Approval backend : {'Aegis (aegistrust.digital) after local hard rules' if aegis else 'local policy file'}")
    print(f"Policy           : {policy['per_payment_cap']} per payment, {policy['daily_cap']} per 24 hours, "
          f"human approval above {policy['approval_threshold']} {policy['currency']}")
    print(f"Allowed vendor   : {vendor} <{vendor_email}>")
    print(f"Decision log     : {log_path}")

    try:
        print("\n1. A small agreed deal: 42.50 for packaging tape")
        before = counter.count
        _show(settle("tape", 42.50, "500 units of packaging tape"), before, counter)

        print("\n2. The agent agrees to 2400.00 for a pallet wrapping machine")
        before = counter.count
        _show(settle("wrapper", 2400.00, "one pallet wrapping machine"), before, counter)

        print("\n3. Same vendor name, different PayPal email (a payee swap)")
        before = counter.count
        _show(settle("swap", 60.00, "200 shipping cartons", email="payments@examp1e-vendor.example"), before, counter)

        print("\n4. 250.00 for corner protectors: above the approval threshold")
        before = counter.count
        held = settle("corners", 250.00, "2000 cardboard corner protectors")
        _show(held, before, counter)
        pending_id = held["decision"]["pending_id"]

        if held["status"] == "needs_approval":
            if aegis:
                print(f"\n   Waiting up to {args.approval_wait}s for the owner to approve Aegis action {pending_id}.")
                print("   The owner approves with their own key, which this process does not hold:")
                print("   curl -X POST $AEGIS_BASE_URL/api/approvals -H \"Authorization: Bearer $AEGIS_OWNER_KEY\" \\")
                print(f"        -H 'Content-Type: application/json' -d '{{\"actionId\":\"{pending_id}\",\"approve\":true}}'")
            elif args.manual_approval:
                print(f"\n   Waiting up to {args.approval_wait}s. In a second terminal run:")
                print(f"   python spend_approval.py --log \"{log_path}\" approve {pending_id}")
            else:
                print("\n   [simulated human] The demo now plays the approver. Run with --manual-approval")
                print("   to do this step yourself.")
                spend_approval.record_human_decision(pending_id, True, "demo-approver (simulated)", log_path=log_path)
                _say("human", f"approved {pending_id}")

            print("\n   The agent asks again for the same payment:")
            before = counter.count
            deadline = time.time() + (args.approval_wait if (aegis or args.manual_approval) else 0)
            released = settle("corners", 250.00, "2000 cardboard corner protectors")
            while released["status"] == "needs_approval" and time.time() < deadline:
                time.sleep(5)
                released = settle("corners", 250.00, "2000 cardboard corner protectors")
            _show(released, before, counter)

            print("\n   The agent tries to settle that same deal once more:")
            before = counter.count
            _show(settle("corners", 250.00, "2000 cardboard corner protectors"), before, counter)

        print("\n5. Another 320.00 deal the same day")
        before = counter.count
        _show(settle("film", 320.00, "40 rolls of stretch film"), before, counter)

        print("\n6. The check itself breaks (policy file missing): it must refuse")
        before = counter.count
        _show(settle("broken", 20.00, "100 address labels", policy_file=str(workdir / "missing.json")), before, counter)

        print("\n7. The decision log")
        ok, message = spend_approval.DecisionLog(log_path).verify()
        _say("verify", ("OK: " if ok else "TAMPERED: ") + message)
        tampered = workdir / "tampered_copy.jsonl"
        tampered.write_text(
            Path(log_path).read_text(encoding="utf-8").replace('"amount":"2400.00"', '"amount":"24.00"', 1),
            encoding="utf-8",
        )
        ok_copy, message_copy = spend_approval.DecisionLog(tampered).verify()
        _say("tamper", "a copy with one amount edited from 2400.00 to 24.00: "
             + ("still verifies (unexpected)" if ok_copy else f"detected, {message_copy}"))
    finally:
        counter.uninstall()

    print("\n" + "=" * 78)
    print(f"Requests sent to PayPal in total: {counter.count}" + ("  (dry run)" if dry_run else "  (sandbox)"))
    print(f"Inspect the log: python spend_approval.py --log \"{log_path}\" show")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
