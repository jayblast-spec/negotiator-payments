"""The local approval page: dry run only, real decisions, real log."""

import http.client
import json
import shutil
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

import approval_ui
import spend_approval

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    monkeypatch.delenv("AEGIS_AGENT_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    policy = tmp_path / "spend_policy.json"
    shutil.copy(REPO / "spend_policy.json", policy)
    approval_ui.STATE.update(log=str(tmp_path / "log.jsonl"), policy=str(policy), flash=[], own_log=True)

    def no_network(*args, **kwargs):
        raise AssertionError("the approval page must never call PayPal")

    monkeypatch.setattr("settle_deal.create_payout", no_network)
    return approval_ui


def _preset(ui, key):
    return next(p for p in ui.PRESETS if p["key"] == key)


def _settle(ui, key):
    p = _preset(ui, key)
    ui.settle(p["vendor"], p["email"], p["purpose"], p["amount"])
    return ui.STATE["flash"][0]


def test_small_deal_is_paid_as_dry_run(ui):
    flash = _settle(ui, "tape")
    assert flash["kind"] == "ok" and "dry run" in flash["title"]
    records = spend_approval.DecisionLog(ui.STATE["log"]).read()
    assert [r.get("status") for r in records if r["type"] == "payout"] == ["dry_run"]


def test_refusals_and_payee_swap(ui):
    assert _settle(ui, "machine")["kind"] == "bad"
    assert "payee swap" in _settle(ui, "swap")["detail"]


def test_approve_releases_exactly_once(ui):
    assert _settle(ui, "corners")["kind"] == "warn"
    pending = spend_approval.list_pending(log_path=ui.STATE["log"])
    assert len(pending) == 1
    ui.decide(pending[0]["pending_id"], True, "Tester")
    assert "dry run" in ui.STATE["flash"][0]["title"]
    assert spend_approval.list_pending(log_path=ui.STATE["log"]) == []
    assert "already paid" in _settle(ui, "corners")["title"]


def test_refuse_records_a_human_no(ui):
    _settle(ui, "corners")
    pending_id = spend_approval.list_pending(log_path=ui.STATE["log"])[0]["pending_id"]
    ui.decide(pending_id, False, "Tester")
    last = spend_approval.DecisionLog(ui.STATE["log"]).read()[-1]
    assert last["type"] == "approval" and last["approved"] is False


def test_page_renders_banner_and_escapes(ui):
    ui.settle("<script>x</script>", "vendor@example.com", "tape", "10.00")
    page = ui.render()
    assert "DRY RUN" in page and "<script>x</script>" not in page


def test_page_flags_a_tampered_log(ui):
    _settle(ui, "tape")
    path = Path(ui.STATE["log"])
    lines = path.read_text().splitlines()
    record = json.loads(lines[0])
    record["intent"]["amount"] = "4.25"
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    assert "TAMPERED" in ui.render()


def test_boxes_alone_explains_the_daily_cap(ui):
    flash = _settle(ui, "boxes")
    assert flash["kind"] == "warn"
    assert "612.50" in flash["detail"] and "daily cap" in flash["detail"]


def test_boxes_break_the_daily_cap_after_tape_and_approved_corners(ui):
    _settle(ui, "tape")
    _settle(ui, "corners")
    pending_id = spend_approval.list_pending(log_path=ui.STATE["log"])[0]["pending_id"]
    ui.decide(pending_id, True, "Tester")
    flash = _settle(ui, "boxes")
    assert flash["kind"] == "bad" and "daily cap" in flash["detail"]


def test_reset_leaves_the_old_log_and_clears_the_story(ui):
    _settle(ui, "tape")
    previous = ui.STATE["log"]
    ui.handle_post("/reset", {})
    assert ui.STATE["log"] != previous and Path(previous).exists()
    assert [item["title"] for item in ui.STATE["flash"]] == ["Fresh demo log"]
    assert spend_approval.DecisionLog(ui.STATE["log"]).read() == []
    assert "TAMPERED" not in ui.render()


def test_missing_action_does_not_refuse(ui):
    _settle(ui, "corners")
    pending_id = spend_approval.list_pending(log_path=ui.STATE["log"])[0]["pending_id"]
    ui.handle_post("/decide", {"pending_id": pending_id, "approver": "Tester"})
    assert spend_approval.list_pending(log_path=ui.STATE["log"])
    assert ui.STATE["flash"][0]["title"] == "No decision recorded"
    assert spend_approval.DecisionLog(ui.STATE["log"]).read()[-1]["type"] == "decision"


def test_deciding_twice_does_not_call_it_a_refusal(ui):
    _settle(ui, "corners")
    pending_id = spend_approval.list_pending(log_path=ui.STATE["log"])[0]["pending_id"]
    ui.handle_post("/decide", {"pending_id": pending_id, "action": "approve", "approver": "Tester"})
    ui.handle_post("/decide", {"pending_id": pending_id, "action": "approve", "approver": "Tester"})
    assert ui.STATE["flash"][0]["title"] == "No decision recorded"
    assert "dry run" in ui.STATE["flash"][1]["title"]


def test_unknown_preset_does_not_settle(ui):
    ui.handle_post("/propose", {"preset": "nope"})
    assert ui.STATE["flash"][0]["title"] == "Unknown deal"
    assert spend_approval.DecisionLog(ui.STATE["log"]).read() == []


def test_token_check_rejects_non_ascii_without_raising():
    assert approval_ui.token_ok("caf\u00e9") is False
    assert approval_ui.token_ok("") is False
    assert approval_ui.token_ok(approval_ui.TOKEN) is True


@pytest.fixture()
def httpd(ui):
    server = approval_ui.ThreadingHTTPServer(("127.0.0.1", 0), approval_ui.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(server, method, path, body=None, headers=None):
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        status = response.status
        header_map = {key.lower(): value for key, value in response.getheaders()}
    finally:
        conn.close()
    return status, header_map, payload


def test_http_rejects_a_bad_post(httpd):
    body = "token=caf\u00e9&preset=tape".encode("utf-8")
    status, _, _ = _request(httpd, "POST", "/propose", body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    })
    assert status == 403

    for length in ("nope", "-1", "100000"):
        status, _, _ = _request(httpd, "POST", "/propose", b"", {"Content-Length": length})
        assert status == 400

    status, headers, _ = _request(httpd, "GET", "/favicon.ico")
    assert status == 204 and headers["x-content-type-options"] == "nosniff"


def test_http_approve_flow(ui, httpd):
    status, headers, page = _request(httpd, "GET", "/")
    assert status == 200 and b"DRY RUN" in page and b"<script>x</script>" not in page
    assert headers["x-frame-options"] == "DENY"

    body = urlencode({"token": approval_ui.TOKEN, "preset": "corners"}).encode()
    status, headers, _ = _request(httpd, "POST", "/propose", body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    })
    assert status == 303 and headers["location"] == "/"
    pending = spend_approval.list_pending(log_path=ui.STATE["log"])
    assert len(pending) == 1

    body = urlencode({
        "token": approval_ui.TOKEN,
        "pending_id": pending[0]["pending_id"],
        "action": "approve",
        "approver": "Tester",
    }).encode()
    status, _, _ = _request(httpd, "POST", "/decide", body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    })
    assert status == 303
    assert spend_approval.list_pending(log_path=ui.STATE["log"]) == []
    records = spend_approval.DecisionLog(ui.STATE["log"]).read()
    assert [record.get("status") for record in records if record["type"] == "payout"] == ["dry_run"]
    status, _, page = _request(httpd, "GET", "/")
    assert status == 200 and b"Paid (dry run)" in page
