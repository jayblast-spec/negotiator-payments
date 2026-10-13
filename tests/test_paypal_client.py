"""paypal_client: token fetch, payout body shape, retries and idempotency."""

from __future__ import annotations

import httpx
import pytest

import paypal_client
from conftest import PAYOUT_URL, TOKEN_URL


def test_token_fetch_uses_client_credentials(http, paypal_creds):
    http.paypal_ready()

    token = paypal_client._get_access_token()

    assert token == "test-access-token"
    (call,) = http.calls
    assert call["method"] == "POST"
    assert call["url"] == TOKEN_URL
    assert call["data"] == {"grant_type": "client_credentials"}
    assert call["auth"] == ("test-client-id", "test-client-secret")


def test_token_fetch_without_credentials_makes_no_request(http):
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT_ID"):
        paypal_client._get_access_token()
    assert http.calls == []


def test_token_fetch_error_does_not_leak_the_secret(http, paypal_creds):
    http.on("POST", TOKEN_URL, (401, {"error": "invalid_client"}))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        paypal_client._get_access_token()
    assert "test-client-secret" not in str(raised.value)


def test_payout_body_shape(http, paypal_creds):
    http.paypal_ready()

    result = paypal_client.create_payout("vendor@example.com", 250, "USD", "Thanks", sender_batch_id="np-abc")

    assert result["batch_header"]["payout_batch_id"] == "BATCH123"
    (call,) = http.to(PAYOUT_URL)
    assert call["json"] == {
        "sender_batch_header": {
            "sender_batch_id": "np-abc",
            "email_subject": "Payment for negotiated deal",
            "email_message": "Thanks",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": "250.00", "currency": "USD"},
                "receiver": "vendor@example.com",
                "note": "Thanks",
                "sender_item_id": "np-abc",
            }
        ],
    }
    assert call["headers"]["Authorization"] == "Bearer test-access-token"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["PayPal-Request-Id"] == "np-abc"


def test_payout_without_explicit_id_still_gets_one(http, paypal_creds):
    http.paypal_ready()
    paypal_client.create_payout("vendor@example.com", 10.5, "USD", "note")
    body = http.to(PAYOUT_URL)[0]["json"]
    batch_id = body["sender_batch_header"]["sender_batch_id"]
    assert batch_id
    assert body["items"][0]["sender_item_id"] == batch_id
    assert body["items"][0]["amount"]["value"] == "10.50"


def test_retry_after_5xx_reuses_the_same_sender_batch_id(http, paypal_creds):
    http.paypal_ready()
    http.on(
        "POST", PAYOUT_URL,
        (503, {"name": "SERVICE_UNAVAILABLE"}),
        (201, {"batch_header": {"payout_batch_id": "BATCH9", "batch_status": "PENDING"}}),
    )

    result = paypal_client.create_payout("vendor@example.com", 42.5, "USD", "note")

    assert result["batch_header"]["payout_batch_id"] == "BATCH9"
    attempts = http.to(PAYOUT_URL)
    assert len(attempts) == 2
    assert attempts[0]["json"] == attempts[1]["json"]
    ids = {attempt["json"]["sender_batch_header"]["sender_batch_id"] for attempt in attempts}
    assert len(ids) == 1
    assert attempts[0]["headers"]["PayPal-Request-Id"] == attempts[1]["headers"]["PayPal-Request-Id"] == ids.pop()


def test_retry_after_timeout_reuses_the_same_sender_batch_id(http, paypal_creds):
    http.paypal_ready()
    http.on(
        "POST", PAYOUT_URL,
        httpx.ReadTimeout("timed out"),
        (201, {"batch_header": {"payout_batch_id": "BATCH10", "batch_status": "PENDING"}}),
    )

    paypal_client.create_payout("vendor@example.com", 42.5, "USD", "note", sender_batch_id="np-fixed")

    attempts = http.to(PAYOUT_URL)
    assert [a["json"]["sender_batch_header"]["sender_batch_id"] for a in attempts] == ["np-fixed", "np-fixed"]


def test_client_error_is_not_retried(http, paypal_creds):
    http.paypal_ready(payout_reply=(422, {"name": "UNPROCESSABLE_ENTITY"}))
    with pytest.raises(httpx.HTTPStatusError):
        paypal_client.create_payout("vendor@example.com", 42.5, "USD", "note")
    assert len(http.to(PAYOUT_URL)) == 1


def test_gives_up_after_max_attempts(http, paypal_creds):
    http.paypal_ready(payout_reply=(500, {"name": "INTERNAL_SERVER_ERROR"}))
    with pytest.raises(httpx.HTTPStatusError):
        paypal_client.create_payout("vendor@example.com", 42.5, "USD", "note", max_attempts=3)
    assert len(http.to(PAYOUT_URL)) == 3


def test_refuses_a_non_sandbox_host(http, paypal_creds, monkeypatch):
    monkeypatch.setattr(paypal_client, "PAYPAL_API_BASE", "https://api-m.paypal.com")
    with pytest.raises(RuntimeError, match="not the PayPal sandbox"):
        paypal_client.create_payout("vendor@example.com", 42.5, "USD", "note")
    assert http.calls == []
