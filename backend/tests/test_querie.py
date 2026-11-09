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
    assert "Zápisom si rezervujete úvodnú cenu" in html
    assert 'data-deadline="' in html
    assert "bez DPH" in html
    assert "Pokračovať na zadanie karty" not in html


def test_founder_page_has_offer_countdown_and_transparency(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 10, 9))
    html = client.get("/querie/").text
    assert 'data-signup="checkout"' in html
    assert "104,25" in html and 'data-deadline="' in html
    assert "Dnes neplatíte nič. Pripomenieme sa 3 dni pred prvou platbou. Zrušíte kedykoľvek." in html
    assert "24. 11. 2026" in html  # first charge = 10 Nov + 14 days
    assert 'id="signup-done"' not in html


def test_form_is_in_dialog_with_three_promises_beside_price(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 20, 9))
    html = client.get("/querie/").text
    assert html.index('<dialog id="signup-dialog"') < html.index('id="signup-form"') < html.index("</dialog>")
    assert "data-open-signup" in html
    promises = html[html.index('class="assure-list"'):html.index("</ol>")]
    assert "Prvá platba až 4. 12. 2026" in promises.replace("\xa0", " ")  # 20 Nov + 14 days
    assert "3 dni pred koncom skúšobného obdobia" in promises
    assert "7 dní od platby" in promises


def test_founder_offer_gone_after_deadline(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 16, 0, 0, 1))
    html = client.get("/querie/").text
    assert 'data-signup="checkout"' in html
    for leftover in ("104,25", "Úvodn", "úvodn", 'data-deadline="'):
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


def test_welcome_page_is_noindex(client):
    resp = client.get("/querie/vitajte")
    assert resp.status_code == 200 and "noindex" in resp.text


def test_old_privacy_page_redirects_to_shared_policy(client):
    resp = client.get("/querie/ochrana-osobnych-udajov", follow_redirects=False)
    assert resp.status_code == 301 and resp.headers["location"] == "/ochrana-osobnych-udajov"


def test_footer_links_all_legal_documents(client):
    html = client.get("/querie/").text
    for url in ("/vop", "/zmluva-o-spracuvani-udajov", "/ochrana-osobnych-udajov", "/sprostredkovatelia"):
        assert f'href="{url}"' in html


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
    assert f'href="{qc.DPA_URL}"' in html and "Zmluvou o spracúvaní osobných údajov" in html
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


# ── yearly billing ───────────────────────────────────────────────────────────
def test_annual_prices():
    assert offer.annual_total() == offer.Decimal("1428")
    assert offer.annual_saving() == offer.Decimal("240")
    assert offer.annual_saving_percent() == 14


def test_annual_switch_shown_once_price_exists(client, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 20))
    monkeypatch.setattr(settings, "QUERIE_STRIPE_ANNUAL_PRICE_ID", None)
    assert 'name="billing"' not in client.get("/querie/").text
    monkeypatch.setattr(settings, "QUERIE_STRIPE_ANNUAL_PRICE_ID", "price_querie_year")
    html = client.get("/querie/").text
    assert 'value="annual" form="signup-form"' in html and 'data-billing="monthly"' in html
    for amount in ("119", "1428", "240"):
        assert offer.eur(offer.Decimal(amount)) in html


def test_annual_switch_shown_on_waitlist_as_pricing_info(client, monkeypatch):
    freeze(monkeypatch, at(2026, 10, 30))
    monkeypatch.setattr(settings, "QUERIE_STRIPE_ANNUAL_PRICE_ID", None)
    html = client.get("/querie/").text
    assert 'value="annual"' in html
    assert "mesačného predplatného" in html  # founder discount is monthly-only


def test_annual_checkout_uses_yearly_price_and_never_the_coupon(client, monkeypatch, stripe_calls):
    freeze(monkeypatch, at(2026, 11, 12, 15))  # founder phase
    monkeypatch.setattr(settings, "QUERIE_STRIPE_ANNUAL_PRICE_ID", "price_querie_year")
    assert client.post("/querie/api/checkout", json=signup(billing="annual")).status_code == 200
    params = stripe_calls[0]
    assert params["line_items"][0]["price"] == "price_querie_year"
    assert "discounts" not in params
    assert params["metadata"]["billing"] == "annual" and params["metadata"]["founder"] == "0"
    assert params["subscription_data"]["trial_period_days"] == 14


def test_annual_checkout_refused_without_yearly_price(client, monkeypatch, stripe_calls):
    freeze(monkeypatch, at(2026, 11, 20))
    monkeypatch.setattr(settings, "QUERIE_STRIPE_ANNUAL_PRICE_ID", None)
    assert client.post("/querie/api/checkout", json=signup(billing="annual")).status_code == 409
    assert client.post("/querie/api/checkout", json=signup(billing="weekly")).status_code == 422
    assert stripe_calls == []


def test_annual_reminder_states_yearly_amount(monkeypatch, sent):
    import asyncio
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", None)
    sub = {
        "id": "sub_7", "status": "trialing", "cancel_at_period_end": False,
        "trial_end": int(at(2026, 11, 23, 10).timestamp()), "customer": "cus_7",
        "items": {"data": [{"quantity": 2}]},
        "metadata": {"product": "querie", "email": "eva@example.sk", "billing": "annual", "founder": "0"},
    }
    asyncio.run(billing.handle_event("customer.subscription.trial_will_end", sub))
    (mail,) = sent
    eur = lambda v: offer.eur(offer.Decimal(v))  # noqa: E731
    assert f"{eur(2856)} bez DPH (2 × {eur(1428)}) za celý rok vopred" in mail.body


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


# ── PDF report page ──────────────────────────────────────────────────────────
def test_report_page_and_slash_redirect(client):
    html = client.get("/querie/pdf_report").text
    assert "predávať viac nehnuteľností" in html and "data-report" in html and "/querie/api/report" in html
    resp = client.get("/querie/pdf_report/?utm_source=facebook", follow_redirects=False)
    assert resp.status_code == 308 and resp.headers["location"] == "/querie/pdf_report?utm_source=facebook"


def test_report_request_stores_lead_and_emails_link(client, sent):
    # No consent tick is needed for the PDF itself.
    resp = client.post("/querie/api/report", json={
        "email": " Eva@Example.SK ", "attribution": {"utm_source": "facebook", "x": "y"},
    })
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert url.startswith("/querie/pdf_report/stiahnut?t=")
    (lead,) = db.all_report_leads()
    assert lead["email"] == "eva@example.sk" and lead["consent_version"] == qc.REPORT_CONSENT_VERSION
    assert lead["marketing"] == 0
    assert "facebook" in lead["attribution"] and '"x"' not in lead["attribution"]
    (mail,) = sent
    assert mail.to == "eva@example.sk" and f"{qc.SITE_URL}{url}" in mail.body


def test_report_marketing_opt_in_is_kept(client, sent):
    client.post("/querie/api/report", json={"email": "eva@example.sk", "marketing": True})
    client.post("/querie/api/report", json={"email": "eva@example.sk", "marketing": False})
    assert db.all_report_leads()[0]["marketing"] == 1


def test_report_request_rejects_bad_email(client):
    resp = client.post("/querie/api/report", json={"email": "nie-je-email"})
    assert resp.status_code == 422 and "e-mail" in resp.json()["detail"]
    assert db.all_report_leads() == []


def test_old_report_leads_keep_their_news_consent(in_memory_db, monkeypatch):
    # A database from before the marketing column: report-1 ticked one box for both.
    in_memory_db.execute("DROP TABLE querie_report_leads")
    in_memory_db.execute(
        "CREATE TABLE querie_report_leads (id INTEGER PRIMARY KEY, email TEXT UNIQUE, consent_at TEXT, "
        "consent_version TEXT, attribution TEXT, user_agent TEXT, downloads INTEGER DEFAULT 0, "
        "created_at TEXT, updated_at TEXT)"
    )
    in_memory_db.execute("INSERT INTO querie_report_leads (email, consent_version) VALUES ('a@b.sk', 'report-1+x')")
    monkeypatch.setattr(db, "_initialized", False)
    db.init()
    assert db.all_report_leads()[0]["marketing"] == 1


FB_UA = "Mozilla/5.0 (Linux; Android 14) Chrome/129.0 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/480.0.0.0;]"


@pytest.mark.parametrize("ua, kind", [
    (FB_UA, "facebook"),
    ("Mozilla/5.0 (iPhone) Mobile/15E148 Instagram 350.0", "instagram"),
    ("facebookexternalhit/1.1", "bot"),
    ("Mozilla/5.0 (Windows NT 10.0) Chrome/130.0", "other"),
])
def test_funnel_browser_kind(ua, kind):
    assert querie_router._browser_kind(ua) == kind


def test_report_funnel_counts_steps(client, sent, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "admin-secret")
    freeze(monkeypatch, at(2026, 10, 30, 12, 0))
    fb = {"User-Agent": FB_UA}
    client.get("/querie/pdf_report", headers=fb)
    client.get("/querie/pdf_report", headers=fb)
    for step in ("view", "focus", "submit", "nonsense"):
        assert client.post("/querie/api/report-event", content='{"e": "%s"}' % step, headers=fb).status_code == 204
    assert client.post("/querie/api/report-event", content="not json", headers=fb).status_code == 204
    client.post("/querie/api/report", json={"email": "eva@example.sk"}, headers=fb)

    assert client.get("/admin/querie-report-funnel").status_code == 401
    csv_text = client.get("/admin/querie-report-funnel", headers={"Authorization": "Bearer admin-secret"}).text
    header, row = csv_text.lstrip("﻿").strip().splitlines()
    assert header == "day,browser,served,view,focus,submit,invalid,sent,error"
    assert row == "2026-10-30,facebook,2,1,1,1,0,1,0"


def test_report_download_needs_valid_link_and_counts(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qc, "REPORT_DIR", tmp_path)
    url = client.post("/querie/api/report", json={"email": "eva@example.sk"}).json()["url"]
    assert client.get(url).status_code == 503  # PDF not uploaded yet
    (tmp_path / qc.REPORT_FILE).write_bytes(b"%PDF-1.4 test")
    resp = client.get(url)
    assert resp.status_code == 200 and resp.content == b"%PDF-1.4 test"
    assert "attachment" in resp.headers["content-disposition"]
    assert db.all_report_leads()[0]["downloads"] == 1
    forged = url.replace(url.split(".")[-1], "0" * 32)
    assert client.get(forged).status_code == 400
