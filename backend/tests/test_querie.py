"""Tests for the Querie landing page (querie/).

What matters most here is the rollout promise: the page, the checkout and the
emails must agree on which phase we are in, the founder coupon must never be
applied after its deadline, and Querie's Stripe events must never fall into
ApollonIA's payment handling on the shared webhook.
"""

import datetime
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from billing import router as billing_router
from config import settings
from querie import billing, db, notify, offer
from querie import config as qc
from querie import router as querie_router
from tenants import db as tenants_db

TZ = qc.TZ


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(db, "_initialized", False)
    db.init()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "QUERIE_STRIPE_PRICE_ID", "price_querie")
    monkeypatch.setattr(settings, "QUERIE_FOUNDER_COUPON_ID", "coupon_founder")
    monkeypatch.setattr(settings, "QUERIE_PHASE", "auto")
    monkeypatch.setattr(settings, "QUERIE_META_PIXEL_ID", None)
    monkeypatch.setattr(settings, "QUERIE_TIKTOK_PIXEL_ID", None)
    monkeypatch.setattr(settings, "SESSION_SECRET", "test-signing-key")
    querie_router._hits.clear()
    yield
    querie_router._hits.clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture every Querie email instead of calling Resend."""
    outbox = []

    async def fake_send(to, subject, body):
        outbox.append(SimpleNamespace(to=to, subject=subject, body=body))

    monkeypatch.setattr(notify, "_send", fake_send)
    monkeypatch.setattr(settings, "QUERIE_NOTIFY_EMAIL", "owner@example.com")
    return outbox


def at(*args) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=TZ)


def freeze(monkeypatch, moment: datetime.datetime):
    monkeypatch.setattr(offer, "now", lambda: moment)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(querie_router.router)
    return TestClient(app)


def signup(**overrides):
    body = {
        "name": "Ján Novák",
        "email": "Jan.Novak@Example.sk",
        "phone": "+421 905 123 456",
        "company": "Reality Novák",
        "consent": True,
        "attribution": {"utm_source": "facebook", "evil": "x"},
    }
    body.update(overrides)
    return body


# ── phases ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("moment, phase", [
    (at(2026, 11, 8, 23, 59), "waitlist"),
    (at(2026, 11, 9, 0, 0), "founder"),
    (at(2026, 11, 15, 23, 59, 59), "founder"),
    (at(2026, 11, 16, 0, 0), "trial"),
])
def test_phase_follows_bratislava_dates(moment, phase):
    assert offer.current_phase(moment) == phase


def test_deadline_is_bratislava_not_utc():
    # 23:30 UTC on the 15th is already 00:30 on the 16th in Bratislava (CET).
    late = datetime.datetime(2026, 11, 15, 23, 30, tzinfo=datetime.timezone.utc)
    assert offer.current_phase(late) == "trial"
    assert not offer.founder_open(late)


def test_without_stripe_price_page_stays_on_waitlist(monkeypatch):
    monkeypatch.setattr(settings, "QUERIE_STRIPE_PRICE_ID", None)
    assert offer.current_phase(at(2026, 11, 10)) == "waitlist"


def test_phase_override(monkeypatch):
    monkeypatch.setattr(settings, "QUERIE_PHASE", "trial")
    assert offer.current_phase(at(2026, 11, 1)) == "trial"
    assert not offer.founder_open(at(2026, 11, 1))


def test_money_and_dates_in_slovak_format():
    assert offer.founder_price() == offer.Decimal("104.25")
    assert offer.eur(offer.founder_price()) == "104,25 €"
    assert offer.eur(qc.PRICE_EUR) == "139 €"
    assert offer.date_sk(datetime.date(2026, 11, 23)) == "23. 11. 2026"
    assert offer.weekday_acc_sk(datetime.date(2026, 11, 11)) == "stredu"


def test_first_charge_is_day_15():
    assert offer.first_charge_date(at(2026, 11, 9, 10)) == datetime.date(2026, 11, 23)


# ── page ─────────────────────────────────────────────────────────────────────
def test_waitlist_page(client, monkeypatch):
    freeze(monkeypatch, at(2026, 10, 26, 12))
    html = client.get("/querie/").text
    assert 'data-signup="waitlist"' in html
    assert "/querie/api/waitlist" in html
    assert "Zápisom si rezervujete zakladateľskú cenu" in html
    assert 'data-deadline="' in html
    assert "bez DPH" in html
    assert "Pokračovať na zadanie karty" not in html


def test_founder_page_has_offer_countdown_and_transparency(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 10, 9))
    html = client.get("/querie/").text
    assert 'data-signup="checkout"' in html
    assert "104,25" in html and 'data-deadline="' in html
    assert "Dnes neplatíte nič. Pripomenieme sa 3 dni pred prvou platbou. Zrušíte jedným klikom." in html
    assert "24. 11. 2026" in html  # first charge = 10 Nov + 14 days
    assert 'id="signup-done"' not in html


def test_founder_offer_gone_after_deadline(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 16, 0, 0, 1))
    html = client.get("/querie/").text
    assert 'data-signup="checkout"' in html
    for leftover in ("104,25", "akladateľsk", 'data-deadline="'):
        assert leftover not in html


def test_product_name_comes_from_config(client, monkeypatch):
    monkeypatch.setattr(qc, "PRODUCT_NAME", "Zvonček")
    html = client.get("/querie/").text
    assert "<title>Zvonček" in html
    assert "Querie" not in html


def test_testimonials_hidden_until_provided(client, monkeypatch):
    assert 'id="skusenosti"' not in client.get("/querie/").text
    monkeypatch.setattr(qc, "TESTIMONIALS", [{"quote": "Konečne viem, kto čo chcel.", "name": "Jana Kováčová", "role": "Maklérka"}])
    html = client.get("/querie/").text
    assert 'id="skusenosti"' in html and "Konečne viem" in html and ">JK<" in html


def test_cookie_banner_and_pixels_only_when_configured(client, monkeypatch):
    html = client.get("/querie/").text
    assert 'id="cookie-banner"' not in html
    monkeypatch.setattr(settings, "QUERIE_META_PIXEL_ID", "1234567890")
    html = client.get("/querie/").text
    assert 'id="cookie-banner"' in html
    assert '"meta": "1234567890"' in html
    # The pixel script URL is only inside loadPixels(), never a <script src>.
    assert 'src="https://connect.facebook.net' not in html


def test_redirect_keeps_campaign_query(client):
    resp = client.get("/querie?utm_source=tiktok&ttclid=abc", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/querie/?utm_source=tiktok&ttclid=abc"


def test_privacy_and_welcome_pages_are_noindex(client):
    for path in ("/querie/ochrana-osobnych-udajov", "/querie/vitajte"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "noindex" in resp.text


# ── waitlist API ─────────────────────────────────────────────────────────────
def test_waitlist_stores_and_notifies(client, sent):
    resp = client.post("/querie/api/waitlist", json=signup())
    assert resp.status_code == 200
    row = db.get_by_email("jan.novak@example.sk")
    assert row["status"] == "waitlist"
    assert row["consent_version"] == qc.CONSENT_VERSION
    assert '"utm_source": "facebook"' in row["attribution"] and "evil" not in row["attribution"]
    assert [m.to for m in sent] == ["owner@example.com"]
    assert "Ján Novák" in sent[0].body


def test_waitlist_requires_consent(client, sent):
    resp = client.post("/querie/api/waitlist", json=signup(consent=False))
    assert resp.status_code == 422
    assert "súhlasu" in resp.json()["detail"]
    assert db.all_signups() == []


def test_trial_form_asks_for_terms_and_privacy(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 20))
    html = client.get("/querie/").text
    assert f'href="{qc.TERMS_URL}"' in html and "Všeobecnými obchodnými podmienkami" in html
    resp = client.post("/querie/api/checkout", json=signup(consent=False))
    assert resp.status_code == 422 and "obchodnými podmienkami" in resp.json()["detail"]


@pytest.mark.parametrize("field, value", [("email", "nie-je-email"), ("phone", "123"), ("name", "  ")])
def test_waitlist_rejects_bad_input_in_slovak(client, field, value):
    resp = client.post("/querie/api/waitlist", json=signup(**{field: value}))
    assert resp.status_code == 422
    assert "Skontrolujte" in resp.json()["detail"] or "Vyplňte" in resp.json()["detail"]


def test_waitlist_honeypot_drops_silently(client, sent):
    resp = client.post("/querie/api/waitlist", json=signup(website="http://spam"))
    assert resp.status_code == 200
    assert db.all_signups() == [] and sent == []


def test_waitlist_signing_up_twice_keeps_one_row(client, sent):
    client.post("/querie/api/waitlist", json=signup())
    client.post("/querie/api/waitlist", json=signup(email="jan.novak@example.sk", phone="+421 911 000 111"))
    rows = db.all_signups()
    assert len(rows) == 1 and rows[0]["phone"] == "+421 911 000 111"


def test_rate_limit(client, sent):
    for _ in range(querie_router._RATE_MAX):
        assert client.post("/querie/api/waitlist", json=signup()).status_code == 200
    assert client.post("/querie/api/waitlist", json=signup()).status_code == 429


# ── checkout ─────────────────────────────────────────────────────────────────
@pytest.fixture
def stripe_calls(monkeypatch):
    calls = []

    def fake_create(**params):
        calls.append(params)
        return SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.com/c/cs_test_1")

    monkeypatch.setattr(billing.stripe.checkout.Session, "create", fake_create)
    return calls


def test_checkout_refused_during_waitlist(client, monkeypatch, stripe_calls):
    freeze(monkeypatch, at(2026, 11, 1))
    assert client.post("/querie/api/checkout", json=signup()).status_code == 409
    assert stripe_calls == []


def test_founder_checkout_applies_coupon_and_trial(client, monkeypatch, stripe_calls):
    freeze(monkeypatch, at(2026, 11, 12, 15))
    resp = client.post("/querie/api/checkout", json=signup())
    assert resp.json() == {"url": "https://checkout.stripe.com/c/cs_test_1"}
    params = stripe_calls[0]
    assert params["discounts"] == [{"coupon": "coupon_founder"}]
    assert params["subscription_data"]["trial_period_days"] == 14
    assert params["subscription_data"]["metadata"]["product"] == "querie"
    assert params["payment_method_collection"] == "always"
    assert params["line_items"][0]["price"] == "price_querie"
    assert params["metadata"]["terms_accepted"].startswith(qc.CONSENT_VERSION)
    assert "26. 11. 2026" in params["custom_text"]["submit"]["message"]
    row = db.get_by_email("jan.novak@example.sk")
    assert row["status"] == "checkout_started" and row["stripe_session_id"] == "cs_test_1" and row["founder"] == 1


def test_checkout_after_deadline_has_no_coupon(client, monkeypatch, stripe_calls):
    freeze(monkeypatch, at(2026, 11, 16, 0, 0, 5))
    client.post("/querie/api/checkout", json=signup())
    assert "discounts" not in stripe_calls[0]
    assert stripe_calls[0]["metadata"]["founder"] == "0"


# ── webhook ──────────────────────────────────────────────────────────────────
def _webhook_app(monkeypatch, event):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setattr(billing_router.stripe.Webhook, "construct_event", lambda *a, **k: event)
    app = FastAPI()
    app.include_router(billing_router.router)
    return TestClient(app)


def test_querie_checkout_event_skips_apollonia_handling(monkeypatch, sent):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", None)  # no Stripe re-fetch in tests
    apollonia = []

    async def apollonia_notification(*args, **kwargs):
        apollonia.append(args)

    monkeypatch.setattr(billing_router, "_send_signup_notification", apollonia_notification)
    monkeypatch.setattr(billing_router, "_send_customer_confirmation", apollonia_notification)
    db.upsert_signup(email="jan@example.sk", name="Ján", phone="0905123456", company=None,
                     status="checkout_started", consent_version="v")
    db.set_checkout_session("jan@example.sk", "cs_1", founder=True)

    event = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "customer_email": "jan@example.sk", "customer": "cus_1", "subscription": "sub_1",
        "metadata": {"product": "querie", "name": "Ján", "founder": "1"},
    }}}
    resp = _webhook_app(monkeypatch, event).post("/webhook", content=b"{}")
    assert resp.status_code == 200
    assert apollonia == []
    assert db.get_by_email("jan@example.sk")["status"] == "trial_started"
    welcome = next(m for m in sent if m.to == "jan@example.sk")
    assert "Dnes ste nezaplatili nič" in welcome.body
    assert "104,25" in welcome.body
    assert "/querie/zrusit?t=sub_1." in welcome.body


def test_apollonia_checkout_event_still_handled(monkeypatch):
    seen = []

    async def apollonia_notification(*args, **kwargs):
        seen.append(args)

    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", None)
    monkeypatch.setattr(billing_router, "_send_signup_notification", apollonia_notification)
    monkeypatch.setattr(billing_router, "_send_customer_confirmation", apollonia_notification)
    event = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_2", "customer_email": "studio@example.it", "metadata": {"plan": "Base"},
    }}}
    assert _webhook_app(monkeypatch, event).post("/webhook", content=b"{}").status_code == 200
    assert len(seen) == 2


def test_trial_will_end_sends_reminder(monkeypatch, sent):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", None)
    trial_end = int(at(2026, 11, 23, 10).timestamp())
    sub = {
        "id": "sub_9", "status": "trialing", "cancel_at_period_end": False, "trial_end": trial_end,
        "customer": "cus_9", "items": {"data": [{"quantity": 2}]},
        "metadata": {"product": "querie", "email": "eva@example.sk", "name": "Eva", "founder": "0"},
    }
    import asyncio
    asyncio.run(billing.handle_event("customer.subscription.trial_will_end", sub))
    (mail,) = sent
    assert mail.to == "eva@example.sk"
    assert "23. 11. 2026" in mail.subject
    assert "278 € bez DPH (2 × 139 €)" in mail.body
    assert "/querie/zrusit?t=sub_9." in mail.body


def test_no_reminder_for_cancelled_trial(monkeypatch, sent):
    import asyncio
    sub = {"id": "sub_9", "status": "trialing", "cancel_at_period_end": True, "metadata": {"product": "querie"}}
    asyncio.run(billing.handle_event("customer.subscription.trial_will_end", sub))
    assert sent == []


# ── cancel links + admin ─────────────────────────────────────────────────────
def test_cancel_token_roundtrip_and_tamper():
    token = billing.manage_token("sub_123")
    assert billing.verify_token(token) == "sub_123"
    assert billing.verify_token(token.replace("sub_123", "sub_124")) is None
    assert billing.verify_token("sub_123.deadbeef") is None


def test_bad_cancel_link_shows_contact(client):
    resp = client.get("/querie/zrusit?t=sub_1.forged")
    assert resp.status_code == 400
    assert qc.CONTACT_EMAIL in resp.text


def test_admin_export_requires_token(client, monkeypatch, sent):
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "admin-secret")
    client.post("/querie/api/waitlist", json=signup())
    assert client.get("/admin/querie-signups").status_code == 401
    resp = client.get("/admin/querie-signups", headers={"Authorization": "Bearer admin-secret"})
    assert resp.status_code == 200
    assert "jan.novak@example.sk" in resp.text
