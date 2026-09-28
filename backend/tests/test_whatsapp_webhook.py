"""The Meta WhatsApp webhook (routers/whatsapp.py): the subscription handshake,
signature checking, and — its reason to exist — a failed delivery landing in
the logs with Meta's error code."""

import hashlib
import hmac
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from routers import whatsapp as webhook

SECRET = "app-secret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRET)
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def _signed_post(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=" + sig, "Content-Type": "application/json"},
    )


def _status_payload(status, errors=None):
    event = {"id": "wamid.X", "status": status, "recipient_id": "393330000001"}
    if errors:
        event["errors"] = errors
    return {"entry": [{"changes": [{"value": {"statuses": [event]}}]}]}


def test_handshake_echoes_the_challenge(client):
    resp = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"},
    )
    assert resp.status_code == 200
    assert resp.text == "42"


def test_handshake_with_wrong_token_is_refused(client):
    resp = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
    )
    assert resp.status_code == 403


def test_unsigned_or_forged_events_are_refused(client):
    assert _signed_post(client, _status_payload("delivered"), secret="wrong").status_code == 401
    assert client.post("/whatsapp/webhook", json=_status_payload("delivered")).status_code == 401


def test_failed_delivery_is_logged_with_metas_reason(client, caplog):
    errors = [{
        "code": 131042,
        "title": "Business eligibility payment issue",
        "error_data": {"details": "No valid payment method"},
    }]
    with caplog.at_level(logging.INFO, logger=webhook.logger.name):
        resp = _signed_post(client, _status_payload("failed", errors))

    assert resp.status_code == 200
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = record.getMessage()
    assert "131042" in message and "No valid payment method" in message
    assert "wamid.X" in message


def test_delivered_is_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger=webhook.logger.name):
        _signed_post(client, _status_payload("delivered"))

    assert any("delivered" in r.getMessage() for r in caplog.records)
