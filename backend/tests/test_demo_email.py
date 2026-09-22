"""Demo tenants ask the caller for an e-mail address (a trial of voice capture
before any client relies on it). What must hold:

- Only the demo tenants get the prompt section and the tool field. A
  production tenant that saw either could start asking for e-mails.
- The address reaches the agent: it is printed in the lead email, both for a
  qualified lead (record_caller_info) and for a message (leave_message).
"""

import pytest

from call import live
from call import router
from config import settings

_DEMO_IT = "+390200000001"
_DEMO_SK = "+421200000002"
_DEMO_LIVE = "+421200000003"


@pytest.fixture(autouse=True)
def _demo_numbers(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", _DEMO_IT)
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER_SK", _DEMO_SK)
    monkeypatch.setattr(settings, "LIVE_DEMO_NUMBER_SK", _DEMO_LIVE)


def _tenant(number, locale="sk", engine="realtime"):
    return {
        "id": "t1",
        "agency_name": "Agentúra",
        "agent_name": "Apollonia",
        "twilio_number": number,
        "locale": locale,
        "voice_engine": engine,
    }


def _tool(tools, name):
    return next(t for t in tools if t["name"] == name)


@pytest.mark.parametrize("number", [_DEMO_IT, _DEMO_SK, _DEMO_LIVE])
def test_demo_numbers_collect_email(number):
    assert router._collects_email(_tenant(number))


def test_env_var_demo_fallback_collects_email():
    assert router._collects_email(None)


def test_production_tenant_does_not_collect_email():
    assert not router._collects_email(_tenant("+43720000099"))
    assert not router._collects_email(_tenant(None))


@pytest.mark.parametrize("locale", ["it", "sk"])
def test_accept_config_email_field_only_when_collecting(locale):
    content = router._content(locale)
    on = router._build_accept_config("x", content, [], collect_email=True)
    off = router._build_accept_config("x", content, [])
    for name in ("record_caller_info", "leave_message"):
        assert "email" in _tool(on["tools"], name)["parameters"]["properties"]
        assert "email" not in _tool(off["tools"], name)["parameters"]["properties"]
    # The injection is per call: the module-level schemas stay untouched.
    for tool in content["tools"]:
        assert "email" not in tool["parameters"]["properties"]


@pytest.mark.parametrize("locale", ["it", "sk"])
def test_email_section_orders_local_part_domain_and_readback(locale):
    section = router._content(locale)["demo_email_section"]
    markers = {
        "it": ("nome", "prima della chiocciola", "dopo la chiocciola", "Ripeti"),
        "sk": ("meno", "pred zavináčom", "za zavináčom", "zopakujte"),
    }[locale]
    positions = [section.index(m) for m in markers]
    assert positions == sorted(positions)


def _live_ctx(number):
    return router._CallContext(
        tenant=_tenant(number, engine="live"),
        locale="sk",
        content=router._content("sk"),
        tenant_store=None,
        lead_email="lead@example.com",
        branch_names=[],
        caller="+421900000000",
        caller_number_known=True,
    )


def test_live_demo_gets_email_on_voice_backend_and_tools():
    cfg = live.build_session_config(_live_ctx(_DEMO_LIVE))
    assert "pred zavináčom" in cfg["instructions"]
    responses = cfg["delegation"]["responses"]
    assert "'email'" in responses["instructions"]
    props = _tool(responses["tools"], "record_caller_info")["parameters"]["properties"]
    assert "email" in props
    # The voice model has no tools, so it must not be told about tool fields.
    assert "record_caller_info" not in cfg["instructions"]


def test_live_production_tenant_gets_no_email():
    cfg = live.build_session_config(_live_ctx("+43720000099"))
    assert "zavináč" not in cfg["instructions"]
    responses = cfg["delegation"]["responses"]
    assert "zavináč" not in responses["instructions"]
    props = _tool(responses["tools"], "record_caller_info")["parameters"]["properties"]
    assert "email" not in props


def _session(**extra):
    session = {
        "interested_listings": [],
        "listings_shown": [],
        "caller_info": {},
        "left_message": None,
    }
    session.update(extra)
    return session


@pytest.mark.parametrize("locale,label", [("it", "Email"), ("sk", "E-mail")])
def test_lead_body_shows_email_from_caller_info(locale, label):
    content = router._content(locale)
    session = _session(caller_info={"name": "Ján", "email": "jan.novak@azet.sk"})
    body = router._format_lead_body(content, session, "+421900000000", [], False)
    assert f"{label}: jan.novak@azet.sk" in body


@pytest.mark.parametrize("locale,label", [("it", "Email"), ("sk", "E-mail")])
def test_lead_body_shows_email_from_left_message(locale, label):
    content = router._content(locale)
    session = _session(
        left_message={
            "caller_name": "Ján",
            "email": "jan@gmail.com",
            "message": "Chce ocenenie bytu.",
        }
    )
    body = router._format_lead_body(content, session, "+421900000000", [], False)
    assert f"{label}: jan@gmail.com" in body
