"""
Local approval page for the spend-approval check. DRY RUN ONLY.

    python approval_ui.py              # http://127.0.0.1:8765, fresh demo log
    python approval_ui.py --port 9000
    python approval_ui.py --log /tmp/demo-decisions.jsonl --policy spend_policy.json

What it shows, in one page:
  * the spending policy in force (caps, approval threshold, vendor allowlist)
    and how much of the 24-hour cap is already used
  * "the agent proposes": preset negotiated deals plus a custom form; each one
    goes through settle_deal.settle_negotiated_deal(..., dry_run=True)
  * the pending-approval queue with Approve / Refuse buttons. Approve records
    the human decision, then the agent retries that exact payment once.
  * the hash-chained decision log, newest first, with a live verify badge

Every settlement from this page is forced to dry_run=True: decisions, the
approval step and the decision log are real, but nothing is sent to PayPal,
whatever is in the environment. The server binds to 127.0.0.1 only and every
form carries a per-process token. Standard library only, no extra packages.

By default the log and the policy are temporary copies, so the page never
touches the repo's own files. Passing --log writes to that file: an approval
recorded here is spent by the dry-run retry.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import spend_approval
from settle_deal import settle_negotiated_deal

REPO = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(16)
LOCK = threading.Lock()
MAX_FORM_BYTES = 10_000
_KINDS = {"ok", "bad", "warn"}

STATE: dict = {"log": None, "policy": None, "flash": [], "own_log": True}

# Demo order: allowed, over the per-payment cap, payee swap, needs a human,
# then the deal that breaks the 24-hour cap once the earlier ones are counted.
PRESETS = [
    {"key": "tape", "label": "Packaging tape", "vendor": "Example Vendor Co", "email": "vendor@example.com",
     "purpose": "500 units of packaging tape", "amount": "42.50"},
    {"key": "machine", "label": "Pallet wrapping machine", "vendor": "Example Vendor Co", "email": "vendor@example.com",
     "purpose": "1 pallet wrapping machine", "amount": "2400.00"},
    {"key": "swap", "label": "Payee swap attempt", "vendor": "Example Vendor Co", "email": "payments@vendor-example.co",
     "purpose": "500 units of packaging tape (new bank details)", "amount": "42.00"},
    {"key": "corners", "label": "Corner protectors", "vendor": "Example Vendor Co", "email": "vendor@example.com",
     "purpose": "2000 cardboard corner protectors", "amount": "250.00"},
    {"key": "boxes", "label": "Shipping boxes", "vendor": "Northside Packaging", "email": "ap@northside-packaging.example",
     "purpose": "400 double-wall shipping boxes", "amount": "320.00",
     "early": "On its own this only waits for approval. It breaks the daily cap after packaging tape "
              "is paid and corner protectors are approved: 42.50 + 250.00 + 320.00 = 612.50, and the "
              "24-hour cap is 600.00."},
]

STATUS_TEXT = {
    "dry_run": ("Paid (dry run)", "ok"),
    "paid": ("Paid", "ok"),
    "denied": ("Refused", "bad"),
    "already_paid": ("Refused: already paid", "bad"),
    "needs_approval": ("Waiting for a human", "warn"),
    "payout_failed": ("Payout failed", "bad"),
    "payout_unknown": ("Payout outcome unknown", "warn"),
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def token_ok(supplied: str) -> bool:
    """Constant-time token check. compare_digest rejects non-ASCII str, so compare bytes."""
    if not isinstance(supplied, str):
        return False
    try:
        return secrets.compare_digest(supplied.encode("utf-8"), TOKEN.encode("ascii"))
    except (UnicodeError, TypeError):
        return False


def flash(kind: str, title: str, detail: str = "") -> None:
    STATE["flash"].insert(0, {"kind": kind if kind in _KINDS else "warn", "title": title, "detail": detail, "t": time.time()})
    del STATE["flash"][4:]


def settle(vendor: str, email: str, purpose: str, amount: str) -> None:
    result = settle_negotiated_deal(vendor, email, purpose, amount, "USD", dry_run=True,
                                    policy_path=STATE["policy"], log_path=STATE["log"])
    status = result.get("status") or "denied"
    label, kind = STATUS_TEXT.get(status, (status, "warn"))
    decision = result.get("decision") or {}
    reason = decision.get("reason") or status
    if result.get("error"):
        reason = f"{reason} {result['error']}"
    if result.get("log_error"):
        reason = f"{reason} {result['log_error']}"
    if status == "dry_run":
        memo = str(result.get("memo") or "").strip()
        tail = "Nothing was sent to PayPal."
        reason = f"{reason} Memo: \u201c{memo}\u201d {tail}" if memo else f"{reason} {tail}"
    preset = next((p for p in PRESETS if p["purpose"] == purpose and p["amount"] == str(amount)), None)
    if preset and preset.get("early") and status == "needs_approval":
        reason = f"{reason} {preset['early']}"
    flash(kind, f"{label}: {amount} USD to {vendor}", reason)


def decide(pending_id: str, approve: bool, approver: str) -> None:
    pending = {p["pending_id"]: p for p in spend_approval.list_pending(log_path=STATE["log"])}
    target = pending.get(pending_id)
    spend_approval.record_human_decision(pending_id, approve, approver or "local approver", log_path=STATE["log"])
    if not approve:
        flash("bad", f"Refused by {approver or 'local approver'}", f"{pending_id} will not be paid.")
        return
    flash("ok", f"Approved by {approver or 'local approver'}", f"{pending_id}: the agent now retries this exact payment.")
    if target:
        intent = target["intent"]
        settle(intent["vendor"], intent["recipient_email"], intent["purpose"], intent["amount"])


def handle_post(path: str, form: dict) -> None:
    """Apply one form action. The caller holds LOCK and has already checked the token."""
    path = path.split("?", 1)[0]
    try:
        if path == "/propose":
            preset_key = form.get("preset") or ""
            if preset_key:
                preset = next((p for p in PRESETS if p["key"] == preset_key), None)
                if preset is None:
                    flash("warn", "Unknown deal", "That preset is not on this page.")
                else:
                    settle(preset["vendor"], preset["email"], preset["purpose"], preset["amount"])
            else:
                vendor = form.get("vendor", "").strip()
                email = form.get("email", "").strip()
                purpose = form.get("purpose", "").strip()
                amount = form.get("amount", "").strip()
                if not all((vendor, email, purpose, amount)):
                    flash("warn", "Deal incomplete", "Vendor, PayPal email, what was bought, and a price are all required.")
                else:
                    settle(vendor, email, purpose, amount)
        elif path == "/decide":
            action = form.get("action") or ""
            pending_id = (form.get("pending_id") or "").strip()
            if action not in {"approve", "refuse"} or not pending_id:
                flash("warn", "No decision recorded", "Choose Approve or Refuse on an open request.")
            else:
                decide(pending_id, action == "approve", (form.get("approver") or "").strip())
        elif path == "/reset":
            previous = STATE["log"]
            STATE["flash"].clear()
            fresh_log()
            flash("ok", "Fresh demo log", f"Previous decisions are still in {previous}.")
        else:
            flash("warn", "Unknown action", "That action is not on this page.")
    except spend_approval.ApprovalError as exc:
        # A second click, an expired request, or an unknown id. Not a refusal of the payment.
        flash("warn", "No decision recorded", str(exc))
    except spend_approval.IntentError as exc:
        flash("bad", "Not recorded", str(exc))
    except spend_approval.LogTamperedError as exc:
        flash("bad", "Log tampered", f"{exc}. Every new payment is refused.")
    except Exception as exc:
        print(f"approval page error: {type(exc).__name__}: {exc}", file=sys.stderr)
        flash("bad", "Something went wrong", f"{type(exc).__name__}: {exc}"[:300])


# ---------------------------------------------------------------- rendering

CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Inter,Roboto,Helvetica,Arial,sans-serif;
background:#f4f6fb;color:#13203a;overflow-x:clip}
.banner{position:sticky;top:0;z-index:5;background:#ffb020;color:#2b1a00;text-align:center;font-weight:700;
letter-spacing:.04em;padding:7px 12px;font-size:13px;overflow-wrap:anywhere}
header{background:#0b1e3d;color:#fff;padding:22px 32px}
header h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:#b8c6e3;font-size:14px}
main{max-width:1180px;margin:0 auto;padding:22px 24px 48px;display:grid;grid-template-columns:1fr 1fr;gap:18px;min-width:0}
.card{background:#fff;border:1px solid #dde3ef;border-radius:14px;padding:18px 20px;box-shadow:0 1px 2px rgba(16,32,64,.04);min-width:0}
.wide{grid-column:1/-1}h2{margin:0 0 10px;font-size:16px}
.muted{color:#5b6b88;font-size:13px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.ok{background:#e3f6ea;color:#11663a}.bad{background:#fde7e7;color:#a11d1d}.warn{background:#fff3d6;color:#8a5a00}
.flash{border-radius:10px;padding:10px 14px;margin:0 0 8px;border:1px solid transparent;overflow-wrap:anywhere}
.flash.ok{border-color:#bfe6cd}.flash.bad{border-color:#f5c2c2}.flash.warn{border-color:#f3dba0}
.flash b{display:block}
.scroll{overflow-x:auto;max-width:100%}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #edf0f6;vertical-align:top}
th{color:#5b6b88;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.reason{overflow-wrap:anywhere}
code{font:12px ui-monospace,Menlo,Consolas,monospace;color:#30405e;overflow-wrap:anywhere}
button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:8px 14px;cursor:pointer;white-space:normal}
button:focus-visible,input:focus-visible{outline:2px solid #1d4ed8;outline-offset:2px}
.primary{background:#1d4ed8;color:#fff}.approve{background:#11804a;color:#fff}.refuse{background:#fff;color:#a11d1d;border:1px solid #e7b4b4}
.ghost{background:#eef2fb;color:#1d3b8a}
.presets{display:flex;flex-wrap:wrap;gap:8px;min-width:0}.presets form{margin:0;max-width:100%;min-width:0}
.preset{background:#eef2fb;color:#13203a;text-align:left;padding:9px 12px;max-width:100%}
.preset small{display:block;color:#5b6b88;font-weight:500;overflow-wrap:anywhere}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
input{width:100%;font:inherit;padding:8px 10px;border:1px solid #cdd5e4;border-radius:8px}
label{display:block;font-size:12px;color:#5b6b88;font-weight:600}
.queue-item{border:1px solid #f3dba0;background:#fffaf0;border-radius:12px;padding:12px 14px;margin-bottom:10px}
.queue-item .amt{font-size:20px;font-weight:700}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
.name{max-width:220px;flex:1 1 160px}
.empty{color:#5b6b88;padding:14px;border:1px dashed #cdd5e4;border-radius:12px;text-align:center}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:14px}.kv span:nth-child(odd){color:#5b6b88}
.custom{margin-top:12px}.custom form{margin-top:8px}
.intro{margin:0 0 10px}
footer{grid-column:1/-1;color:#5b6b88;font-size:12px;text-align:center}
@media (max-width:800px){
  main{grid-template-columns:1fr;padding:16px 14px 40px}
  header{padding:18px 16px}
  .grid2{grid-template-columns:1fr}
}
"""

# Set after submit starts so a double click cannot post twice. Disabling the
# button here would drop its name and value, and Approve would be read as Refuse.
PAGE_SCRIPT = """
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (form.dataset.sent === "1") { event.preventDefault(); return; }
  form.dataset.sent = "1";
});
"""


def hidden() -> str:
    return f'<input type="hidden" name="token" value="{esc(TOKEN)}">'


def _clock(value, zone: bool = False) -> str:
    text = "" if value is None else str(value)
    if len(text) >= 19 and text[10] == "T":
        clock = text[11:19]
        return f"{clock} UTC" if zone else clock
    return text


def _spent_label(records: list, policy) -> str:
    try:
        spent = spend_approval._spent_in_window(records, time.time(), "")
    except Exception:
        return "unavailable"
    return f"{spent} of {policy.daily_cap} {policy.currency}"


def render() -> str:
    policy = spend_approval.SpendPolicy.load(STATE["policy"])
    log = spend_approval.DecisionLog(STATE["log"])
    ok, message = log.verify()
    try:
        records = log.read()
    except spend_approval.LogTamperedError:
        records = []
        ok = False
    pending = spend_approval.list_pending(log_path=STATE["log"]) if ok else []
    spent = _spent_label(records, policy) if ok else "not counted, because the log does not verify"

    flashes = "".join(
        f'<div class="flash {esc(f["kind"])}"><b>{esc(f["title"])}</b><span class="muted">{esc(f["detail"])}</span></div>'
        for f in STATE["flash"]
    ) or '<div class="empty">Pick a deal for the agent to settle. Results show up here.</div>'

    preset_buttons = "".join(
        f'<form method="post" action="/propose">{hidden()}'
        f'<input type="hidden" name="preset" value="{esc(p["key"])}">'
        f'<button class="preset" type="submit">{esc(p["amount"])} USD \u00b7 {esc(p["label"])}'
        f'<small>{esc(p["vendor"])} &lt;{esc(p["email"])}&gt;</small></button></form>'
        for p in PRESETS
    )

    if pending:
        queue_items = []
        for record in pending:
            intent = record.get("intent") or {}
            pending_id = record.get("pending_id") or ""
            queue_items.append(
                f'<div class="queue-item"><div class="amt">{esc(intent.get("amount"))} {esc(intent.get("currency"))}</div>'
                f'<div>{esc(intent.get("vendor"))} <span class="muted">&lt;{esc(intent.get("recipient_email"))}&gt;</span></div>'
                f'<div class="muted">For: {esc(intent.get("purpose"))}</div>'
                f'<div class="muted">Why it is held: {esc(record.get("reason"))}</div>'
                f'<div class="muted">Request <code>{esc(pending_id)}</code> \u00b7 expires {_clock(record.get("expires_at"), zone=True)}</div>'
                f'<form method="post" action="/decide" class="row">{hidden()}'
                f'<input type="hidden" name="pending_id" value="{esc(pending_id)}">'
                f'<input class="name" name="approver" placeholder="Your name" aria-label="Your name" '
                f'maxlength="80" autocomplete="name" required>'
                f'<button class="approve" type="submit" name="action" value="approve">Approve this payment</button>'
                f'<button class="refuse" type="submit" name="action" value="refuse">Refuse</button></form></div>'
            )
        queue = "".join(queue_items)
    else:
        queue = '<div class="empty">Nothing is waiting for approval.</div>'

    rows = []
    for record in reversed(records):
        kind = record.get("type")
        intent = record.get("intent") or {}
        if kind == "decision":
            what = record.get("outcome", "")
            cls = {"allow": "ok", "deny": "bad", "needs_approval": "warn"}.get(what, "warn")
            what = {"allow": "allow", "deny": "deny", "needs_approval": "needs approval"}.get(what, what)
        elif kind == "approval":
            approved = record.get("approved") is True
            what = ("approved" if approved else "refused") + f" by {record.get('approver', '')}"
            cls = "ok" if approved else "bad"
        else:
            status = record.get("status", "")
            cls = {"sent": "ok", "dry_run": "ok", "failed": "bad"}.get(status, "warn")
            what = f"payout: {status}"
        detail = record.get("reason") or record.get("detail") or ""
        digest = record.get("hash") or ""
        short = f"{digest[:10]}\u2026" if digest else ""
        rows.append(
            f'<tr><td>{esc(record.get("seq"))}</td><td class="muted">{esc(_clock(record.get("ts")))}</td>'
            f'<td>{esc(kind)}</td><td><span class="pill {cls}">{esc(what)}</span></td>'
            f'<td>{esc(intent.get("amount", ""))} {esc(intent.get("currency", ""))}</td>'
            f'<td>{esc(intent.get("vendor", ""))}</td><td class="muted reason">{esc(detail)}</td>'
            f'<td><code>{esc(short)}</code></td></tr>'
        )
    log_table = (
        '<div class="scroll"><table><tr><th>#</th><th>Time (UTC)</th><th>Type</th><th>Result</th>'
        "<th>Amount</th><th>Vendor</th><th>Reason</th><th>Hash</th></tr>"
        + "".join(rows) + "</table></div>"
    ) if rows else '<div class="empty">The decision log is empty.</div>'
    if ok:
        head = ""
        if records and records[-1].get("hash"):
            head = f" \u00b7 head {esc(records[-1]['hash'][:10])}\u2026"
        badge = f'<span class="pill ok">Chain verifies \u00b7 {len(records)} records{head}</span>'
    else:
        badge = f'<span class="pill bad">TAMPERED: {esc(message)}. Every new payment is refused.</span>'

    allow_entries = json.loads(Path(STATE["policy"]).read_text(encoding="utf-8")).get("vendor_allowlist", [])
    allow = "".join(
        f'<div>{esc(v.get("vendor"))} <span class="muted">&lt;{esc(v.get("paypal_email"))}&gt;</span></div>'
        for v in allow_entries if isinstance(v, dict)
    )
    banner = "DRY RUN \u00b7 decisions, approvals and the log are real \u00b7 nothing is sent to PayPal"
    if STATE.get("own_log") is False:
        banner += " \u00b7 this is the log you passed in: approving here uses up that approval on a dry run"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>negotiator-payments \u00b7 approvals (DRY RUN)</title>
<style>{CSS}</style></head><body>
<div class="banner">{esc(banner)}</div>
<header><h1>negotiator-payments \u00b7 approvals</h1>
<p>The agent negotiated the deal. It still cannot pay a cent outside these rules.</p></header>
<main>
<section class="card"><h2>1 \u00b7 The agent proposes a payment</h2>
<p class="muted intro">Try them in the order shown. Shipping boxes is the daily-cap case: it is refused only after packaging tape is paid and corner protectors are approved.</p>
<div class="presets">{preset_buttons}</div>
<details class="custom"><summary class="muted">Custom deal</summary>
<form method="post" action="/propose" autocomplete="off">{hidden()}
<div class="grid2"><div><label for="vendor">Vendor</label><input id="vendor" name="vendor" maxlength="120" required></div>
<div><label for="email">Vendor PayPal email</label><input id="email" name="email" type="email" maxlength="254" autocomplete="off" required></div>
<div><label for="purpose">What was bought</label><input id="purpose" name="purpose" maxlength="500" required></div>
<div><label for="amount">Agreed price (USD)</label><input id="amount" name="amount" inputmode="decimal" maxlength="20" required></div></div>
<div class="row"><button class="primary" type="submit">Settle this deal</button></div></form></details>
</section>
<section class="card"><h2>What just happened</h2>{flashes}</section>
<section class="card"><h2>2 \u00b7 Waiting for a human</h2>{queue}</section>
<section class="card"><h2>Policy in force</h2><div class="kv">
<span>Per payment cap</span><span>{esc(policy.per_payment_cap)} {esc(policy.currency)}</span>
<span>24-hour cap</span><span>{esc(policy.daily_cap)} {esc(policy.currency)}</span>
<span>Spent in that window</span><span>{esc(spent)}</span>
<span>Human approval above</span><span>{esc(policy.approval_threshold)} {esc(policy.currency)}</span>
<span>Approval expires after</span><span>{esc(policy.approval_ttl_seconds // 60)} minutes</span>
<span>Allowed vendors</span><span>{allow}</span></div></section>
<section class="card wide"><h2>3 \u00b7 Decision log {badge}</h2>{log_table}
<form method="post" action="/reset" class="row">{hidden()}<button class="ghost" type="submit">Start a fresh demo log</button>
<span class="muted">Log file: <code>{esc(STATE['log'])}</code></span></form></section>
<footer>negotiator-payments \u00b7 ARKNET DIGITAL \u00b7 Apache-2.0 \u00b7 local page, bound to 127.0.0.1</footer>
</main>
<script>{PAGE_SCRIPT}</script>
</body></html>"""


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    server_version = "negotiator-payments-ui"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def _send(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _read_form(self) -> dict | None:
        """Parse the body, or send an error and return None. Never reads more than the cap."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, "bad content length", "text/plain")
            return None
        if length < 0 or length > MAX_FORM_BYTES:
            self._send(400, "form is too large", "text/plain")
            return None
        try:
            body = self.rfile.read(length).decode("utf-8")
            parsed = parse_qs(body, keep_blank_values=True, max_num_fields=32)
        except UnicodeDecodeError:
            self._send(400, "form must be utf-8", "text/plain")
            return None
        except ValueError:
            self._send(400, "bad form", "text/plain")
            return None
        return {key: values[0] for key, values in parsed.items() if values}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()
            return
        if path != "/":
            return self._send(404, "not found", "text/plain")
        with LOCK:
            try:
                page = render()
                code = 200
            except Exception as exc:
                page = f"<!doctype html><meta charset='utf-8'><title>approvals</title><pre>{esc(type(exc).__name__)}: {esc(exc)}</pre>"
                code = 500
        self._send(code, page)

    def do_POST(self):
        form = self._read_form()
        if form is None:
            return
        if not token_ok(form.get("token", "")):
            return self._send(403, "bad form token", "text/plain")
        with LOCK:
            handle_post(self.path, form)
        self._redirect()


def fresh_log() -> None:
    folder = tempfile.mkdtemp(prefix="negotiator-payments-ui-")
    STATE["log"] = str(Path(folder) / "spend_decisions.jsonl")
    STATE["own_log"] = True


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local DRY RUN approval page for negotiator-payments.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", default=None, help="decision log (default: a fresh temporary file)")
    parser.add_argument("--policy", default=None, help="policy file (default: a copy of spend_policy.json)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    os.environ.pop("AEGIS_AGENT_KEY", None)  # this page shows the local approval backend only
    if args.policy:
        policy_path = Path(args.policy).resolve()
        if not policy_path.is_file():
            print(f"Policy file not found: {policy_path}", file=sys.stderr)
            return 1
        STATE["policy"] = str(policy_path)
    else:
        folder = tempfile.mkdtemp(prefix="negotiator-payments-policy-")
        STATE["policy"] = str(Path(folder) / "spend_policy.json")
        shutil.copy(REPO / "spend_policy.json", STATE["policy"])
    if args.log:
        STATE["log"] = str(Path(args.log).resolve())
        STATE["own_log"] = False
        print("Using the log you passed in. Approving a payment here records a dry run and uses up that approval.")
    else:
        fresh_log()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"DRY RUN approval page: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    print(f"Decision log: {STATE['log']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
