"""Shared fixtures. No test touches the network: httpx.post and httpx.get are
replaced with a fake that fails on any call a test did not script."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import paypal_client

PAYPAL = "https://api-m.sandbox.paypal.com"
TOKEN_URL = f"{PAYPAL}/v1/oauth2/token"
PAYOUT_URL = f"{PAYPAL}/v1/payments/payouts"
AEGIS = "https://aegistrust.digital"
ENFORCE_URL = f"{AEGIS}/api/enforce"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

POLICY = {
    "currency": "USD",
    "per_payment_cap": "500.00",
    "daily_cap": "600.00",
    "approval_threshold": "100.00",
    "approval_ttl_seconds": 900,
    "vendor_allowlist": [
        {"vendor": "Example Vendor Co", "paypal_email": "vendor@example.com"},
        {"vendor": "Northside Packaging", "paypal_email": "ap@northside-packaging.example"},
    ],
}


class FakeHttp:
    """Scripted stand-in for httpx.post / httpx.get that records every call."""

    def __init__(self) -> None:
        self.calls: list = []
        self._routes: dict = {}

    def on(self, method: str, url: str, *replies) -> None:
        """Queue replies for a URL. A reply is (status, json_body) or an
        exception instance to raise. The last reply repeats."""
        self._routes[(method, url)] = list(replies)

    def _handle(self, method: str, url: str, kwargs: dict):
        url = str(url)
        body = None
        if kwargs.get("json") is not None:
            body = kwargs["json"]
        elif kwargs.get("content") is not None:
            body = json.loads(kwargs["content"])
        self.calls.append({
            "method": method,
            "url": url,
            "json": body,
            "raw": kwargs.get("content"),
            "data": kwargs.get("data"),
            "auth": kwargs.get("auth"),
            "headers": dict(kwargs.get("headers") or {}),
        })
        replies = self._routes.get((method, url))
        if not replies:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, BaseException):
            raise reply
        status, payload = reply
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    def post(self, url, **kwargs):
        return self._handle("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._handle("GET", url, kwargs)

    def to(self, url: str) -> list:
        return [call for call in self.calls if call["url"] == url]

    def paypal_ready(self, payout_reply=None) -> None:
        self.on("POST", TOKEN_URL, (200, {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 32400}))
        self.on("POST", PAYOUT_URL, payout_reply or (201, {
            "batch_header": {
                "payout_batch_id": "BATCH123",
                "batch_status": "PENDING",
                "sender_batch_header": {"sender_batch_id": "echo"},
            }
        }))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_ALLOW_LIVE", "GROQ_API_KEY",
        "AEGIS_AGENT_KEY", "AEGIS_BASE_URL", "AEGIS_SCOPES", "SPEND_POLICY_FILE", "SPEND_LOG_PATH",
        "DEMO_VENDOR", "DEMO_VENDOR_PAYPAL_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paypal_client, "PAYPAL_API_BASE", PAYPAL)
    monkeypatch.setattr(paypal_client, "_sleep", lambda seconds: None)


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "get", fake.get)
    return fake


@pytest.fixture
def paypal_creds(monkeypatch) -> None:
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")


@pytest.fixture
def policy_path(tmp_path: Path) -> str:
    path = tmp_path / "spend_policy.json"
    path.write_text(json.dumps(POLICY), encoding="utf-8")
    return str(path)


@pytest.fixture
def log_path(tmp_path: Path) -> str:
    return str(tmp_path / "spend_decisions.jsonl")
