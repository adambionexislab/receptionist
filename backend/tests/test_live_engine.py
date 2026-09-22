"""Tests for the GPT-Live phone engine (call/live.py, call/live_prompts.py) and
the webhook split that decides which engine answers a call (call/router.py).

The two things that would hurt most if they broke:

1. Both webhooks fire for every call once GPT-Live SIP is on. If both handlers
   acted on the same call, OpenAI would take whichever decision landed first —
   a Realtime tenant could be answered by GPT-Live, or an unknown number
   declined twice. Each handler must act on its own tenants' calls only.

2. The delegation protocol. A function result that is submitted but never
   followed by response.create leaves the backend waiting forever, and the
   caller with it. An end_call result that IS submitted makes the backend
   write a reply, which is spoken into the call over the goodbye.
"""

import asyncio
import json
import re

import pytest

from call import live
from call import router
from call.live_prompts import SK_LIVE

# ── helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    *,
    engine="live",
    locale="sk",
    branch_names=(),
    caller_number_known=True,
    tenant=True,
):
    return router._CallContext(
        tenant=(
            {
                "id": "t-live",
                "agency_name": "Štúdio Demo Live",
                "agent_name": "Apollonia",
                "voice_engine": engine,
                "locale": locale,
            }
            if tenant
            else None
        ),
        locale=locale,
        content=router._content(locale),
        tenant_store=None,
        lead_email="lead@example.com",
        branch_names=list(branch_names),
        caller="+421900000000",
        caller_number_known=caller_number_known,
    )


class FakeStore:
    def __init__(self, listings):
        self.listings = listings
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return list(self.listings)

    def get_by_address(self, query):
        return [l for l in self.listings if query.lower() in l["address"].lower()]


LISTING = {
    "address": "Obchodná 5, Bratislava",
    "zone": "Staré Mesto",
    "type": "affitto",
    "rooms": 2,
    "size_sqm": 55,
    "price": 900,
    "currency": "EUR",
    "available": True,
    "text": "Svetlý byt na treťom poschodí.",
}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _live_call(store=None):
    sent = []

    async def send(event):
        sent.append(event)

    clock = Clock()
    ctx = _ctx()
    session = router._new_call_session("sess_1", ctx)
    call = live.LiveCall(
        "sess_1", session, ctx.content, store or FakeStore([LISTING]), send, clock
    )
    return call, sent, clock, session


def _function_call(name, arguments, call_id="call_1", delegation_id="item_1"):
    return {
        "type": "response.event",
        "delegation_id": delegation_id,
        "event": {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            },
        },
    }


def _completed(delegation_id="item_1"):
    return {
        "type": "response.event",
        "delegation_id": delegation_id,
        "event": {"type": "response.completed", "response": {"status": "completed", "output": []}},
    }


def run(coro):
    return asyncio.run(coro)


# ── Which engine answers ─────────────────────────────────────────────────────


def test_live_tenant_uses_live_engine():
    assert router._uses_live_engine(_ctx(engine="live"))


def test_realtime_tenant_does_not():
    assert not router._uses_live_engine(_ctx(engine="realtime"))


def test_tenant_without_the_column_defaults_to_realtime():
    ctx = _ctx()
    del ctx.tenant["voice_engine"]
    assert not router._uses_live_engine(ctx)


def test_live_tenant_in_a_locale_without_live_prompts_falls_back_to_realtime():
    """Italian has no GPT-Live prompts: the call must still be answered."""
    assert not router._uses_live_engine(_ctx(engine="live", locale="it"))


def test_env_demo_fallback_without_tenant_is_realtime():
    assert not router._uses_live_engine(_ctx(tenant=False))


@pytest.fixture
def no_side_effects(monkeypatch):
    """Record what each handler would do instead of calling OpenAI."""
    calls = {"reject": [], "realtime_tasks": [], "live_tasks": []}

    async def fake_reject(call_id, status_code=603):
        calls["reject"].append(call_id)

    async def fake_run_call(call_id, *args, **kwargs):
        calls["realtime_tasks"].append(call_id)

    async def fake_run_live_call(session_id, *args, **kwargs):
        calls["live_tasks"].append(session_id)

    monkeypatch.setattr(router, "_reject_call", fake_reject)
    monkeypatch.setattr(router, "_run_call", fake_run_call)
    monkeypatch.setattr(live, "run_live_call", fake_run_live_call)
    router._claimed_calls.clear()
    yield calls
    router._claimed_calls.clear()


async def _both_webhooks(ctx, monkeypatch, call_id="c1"):
    monkeypatch.setattr(router, "_resolve_call", lambda cid, headers: ctx)
    await router._handle_realtime_incoming({"call_id": call_id, "sip_headers": []})
    await live.handle_incoming(
        {"type": "sip", "session_id": call_id, "sip_headers": []}
    )
    await asyncio.sleep(0)  # let the created tasks run


def test_live_tenant_call_is_taken_by_live_only(no_side_effects, monkeypatch):
    run(_both_webhooks(_ctx(engine="live"), monkeypatch))
    assert no_side_effects == {"reject": [], "realtime_tasks": [], "live_tasks": ["c1"]}


def test_realtime_tenant_call_is_taken_by_realtime_only(no_side_effects, monkeypatch):
    run(_both_webhooks(_ctx(engine="realtime"), monkeypatch))
    assert no_side_effects == {"reject": [], "realtime_tasks": ["c1"], "live_tasks": []}


def test_unknown_number_is_rejected_exactly_once(no_side_effects, monkeypatch):
    run(_both_webhooks(None, monkeypatch))
    assert no_side_effects == {"reject": ["c1"], "realtime_tasks": [], "live_tasks": []}


def test_redelivered_live_webhook_starts_one_call(no_side_effects, monkeypatch):
    """A second control task would run every tool twice."""
    ctx = _ctx(engine="live")
    monkeypatch.setattr(router, "_resolve_call", lambda cid, headers: ctx)

    async def twice():
        for _ in range(2):
            await live.handle_incoming({"type": "sip", "session_id": "c1", "sip_headers": []})
        await asyncio.sleep(0)

    run(twice())
    assert no_side_effects["live_tasks"] == ["c1"]


def test_deprecated_live_event_name_is_still_routed(no_side_effects, monkeypatch):
    """live.call.incoming has no data.type — it must not be ignored for that."""
    ctx = _ctx(engine="live")
    monkeypatch.setattr(router, "_resolve_call", lambda cid, headers: ctx)

    async def go():
        await live.handle_incoming({"session_id": "c9", "sip_headers": []})
        await asyncio.sleep(0)

    run(go())
    assert no_side_effects["live_tasks"] == ["c9"]


# ── Session config ───────────────────────────────────────────────────────────


def test_session_config_shape():
    cfg = live.build_session_config(_ctx())
    assert cfg["type"] == "live"
    assert cfg["model"] == "gpt-live-1"
    assert cfg["audio"] == {"output": {"voice": "marin"}}
    backend = cfg["delegation"]["responses"]
    assert cfg["delegation"]["type"] == "responses"
    assert backend["parallel_tool_calls"] is False
    assert backend["tool_choice"] == "auto"
    # SIP negotiates the codec, and GPT-Live has no turn detection at all.
    assert "format" not in json.dumps(cfg["audio"])
    assert "turn_detection" not in json.dumps(cfg)


def test_backend_carries_the_same_six_slovak_tools():
    backend = live.build_session_config(_ctx())["delegation"]["responses"]
    names = [t["name"] for t in backend["tools"]]
    assert names == [t["name"] for t in router._content("sk")["tools"]]
    assert len(names) == 6


def test_office_names_go_on_the_backend_tool_and_not_the_module_constant():
    cfg = live.build_session_config(_ctx(branch_names=["Bratislava", "Košice"]))
    leave = next(t for t in cfg["delegation"]["responses"]["tools"] if t["name"] == "leave_message")
    assert leave["parameters"]["properties"]["branch"]["enum"] == ["Bratislava", "Košice"]
    original = next(t for t in router._content("sk")["tools"] if t["name"] == "leave_message")
    assert "branch" not in original["parameters"]["properties"]
    assert "Košice" not in cfg["instructions"]


def test_service_tier_is_omitted_unless_configured(monkeypatch):
    backend = live.build_session_config(_ctx())["delegation"]["responses"]
    assert "service_tier" not in backend
    monkeypatch.setattr(live.settings, "LIVE_BACKEND_SERVICE_TIER", "priority")
    backend = live.build_session_config(_ctx())["delegation"]["responses"]
    assert backend["service_tier"] == "priority"


# ── Prompts ──────────────────────────────────────────────────────────────────


def test_voice_prompt_names_the_tenant_and_declares_it_is_virtual():
    text = live.build_voice_instructions(_ctx())
    assert "Ste Apollonia, virtuálna recepčná Štúdio Demo Live" in text
    assert "POVINNÉ VYHLÁSENIE" in text
    assert "virtuálna asistentka Štúdio Demo Live" in " ".join(text.split())


def test_greeting_cue_points_at_a_heading_the_voice_prompt_has():
    """The greeting instruction sends her to this section by name."""
    greeting = router._content("sk")["greeting_prompt"]
    heading = re.search(r"'# (.+?)'", greeting).group(1)
    assert f"# {heading}" in live.build_voice_instructions(_ctx())


def test_voice_prompt_keeps_the_gpt_live_policy_headings():
    text = live.build_voice_instructions(_ctx())
    for label in (
        "Backchannel policy:",
        "Interruption policy:",
        "Delegation policy:",
        "Backend tools:",
        "Delegate to the backend when:",
        "Do not delegate to the backend when:",
    ):
        assert label in text


def test_voice_prompt_never_names_a_tool():
    """The voice model has no tools; naming them invites it to 'call' one."""
    text = live.build_voice_instructions(_ctx())
    for tool in router._content("sk")["tools"]:
        assert tool["name"] not in text


def test_voice_prompt_drops_the_preamble_section():
    assert "Úvodné frázy" not in live.build_voice_instructions(_ctx())


def test_ending_is_delegated_not_spoken():
    """Same structure as the Realtime fix in test_call_ending: she must not
    compose the goodbye under the full prompt."""
    text = live.build_voice_instructions(_ctx())
    assert "NELÚČTE sa sama" in text


def test_both_prompts_carry_the_date():
    for text in (live.build_voice_instructions(_ctx()), live.build_backend_instructions(_ctx())):
        assert "# Dátum a čas" in text


def test_ask_for_number_only_when_the_number_is_unusable():
    known = _ctx(caller_number_known=True)
    unknown = _ctx(caller_number_known=False)
    assert SK_LIVE["voice_ask_for_number"] not in live.build_voice_instructions(known)
    assert SK_LIVE["voice_ask_for_number"] in live.build_voice_instructions(unknown)
    assert SK_LIVE["backend_number_unknown"] not in live.build_backend_instructions(known)
    assert SK_LIVE["backend_number_unknown"] in live.build_backend_instructions(unknown)


def test_voice_prompt_fits_the_instructions_limit():
    """16,384 tokens. Slovak runs well over 2 characters per token, so this is
    a conservative ceiling that still catches the prompt doubling."""
    text = live.build_voice_instructions(_ctx(caller_number_known=False))
    assert len(text) < 16_384 * 2


# English words that must never reach a Slovak prompt. The policy labels are
# the one sanctioned exception (see live_prompts.py).
_ALLOWED_ENGLISH = (
    "Backchannel policy:",
    "Interruption policy:",
    "Delegation policy:",
    "Backend tools:",
    "Delegate to the backend when:",
    "Do not delegate to the backend when:",
)
_FOREIGN = (
    "caller", "listing", "property", "please", "whether", "the ",
    "chiamante", "immobile", "richiesta", "agente",
)


@pytest.mark.parametrize("which", ["voice", "backend"])
def test_slovak_live_prompts_have_no_foreign_prose(which):
    ctx = _ctx(caller_number_known=False)
    text = (
        live.build_voice_instructions(ctx)
        if which == "voice"
        else live.build_backend_instructions(ctx)
    )
    for label in _ALLOWED_ENGLISH:
        text = text.replace(label, "")
    # Tool and field names are machine identifiers the backend must use.
    for tool in router._content("sk")["tools"]:
        text = text.replace(tool["name"], "")
    lowered = text.lower()
    found = [w for w in _FOREIGN if w in lowered]
    assert not found, found


# ── Delegation protocol ──────────────────────────────────────────────────────


def test_greeting_is_an_instruction_append():
    call, sent, _, _ = _live_call()
    run(call.start())
    assert sent == [{
        "type": "session.instructions.append",
        "event_id": "greeting",
        "delegation_id": None,
        "content": router._content("sk")["greeting_prompt"],
    }]


def test_tool_result_is_submitted_then_backend_continued_after_completion():
    store = FakeStore([LISTING])
    call, sent, _, session = _live_call(store)

    async def go():
        await call.handle(_function_call("search_listings", {"type": "affitto", "zone": "Bratislava"}))
        # The result goes back at once, but the backend is only continued
        # once the response that asked for it has finished.
        assert [e["type"] for e in sent] == ["response.item.create"]
        await call.handle(_completed())

    run(go())
    assert [e["type"] for e in sent] == ["response.item.create", "response.create"]
    output = json.loads(sent[0]["item"]["output"])
    assert sent[0]["item"]["call_id"] == "call_1"
    # The Slovak field names, same projection as the Realtime path.
    assert output[0]["adresa"] == "Obchodná 5, Bratislava"
    assert store.searches == [{"type": "affitto", "zone": "Bratislava"}]
    assert session["listings_shown"] == [LISTING]


def test_completion_without_a_pending_result_does_not_continue():
    call, sent, _, _ = _live_call()
    run(call.handle(_completed()))
    assert sent == []


def test_repeated_function_call_item_runs_the_tool_once():
    store = FakeStore([LISTING])
    call, sent, _, _ = _live_call(store)

    async def go():
        await call.handle(_function_call("search_listings", {}))
        await call.handle(_function_call("search_listings", {}))

    run(go())
    assert len(store.searches) == 1


def test_record_caller_info_fills_the_session():
    call, _, _, session = _live_call()
    run(call.handle(_function_call("record_caller_info", {"name": "Ján", "has_pets": "nie"})))
    assert session["caller_info"] == {"name": "Ján", "has_pets": "nie"}


def test_unknown_tool_still_gets_a_result():
    """Without one the backend cannot continue at all."""
    call, sent, _, _ = _live_call()
    run(call.handle(_function_call("no_such_tool", {})))
    assert sent[0]["type"] == "response.item.create"
    assert "error" in json.loads(sent[0]["item"]["output"])


def test_end_call_requests_the_goodbye_and_submits_no_result():
    call, sent, clock, session = _live_call()

    async def go():
        await call.handle(_function_call("end_call", {}))
        await call.handle(_completed())

    run(go())
    assert sent == [{
        "type": "session.instructions.append",
        "event_id": "farewell",
        "delegation_id": None,
        "content": router._content("sk")["farewell_instruction"],
    }]
    assert session["ending_at"] == clock.t


def test_hangs_up_once_the_goodbye_has_been_said():
    call, _, clock, _ = _live_call()

    async def go():
        await call.handle(_function_call("end_call", {}))
        clock.t += 0.5
        await call.handle({"type": "session.instructions.appended", "client_event_id": "farewell"})
        clock.t += 0.2
        await call.handle({"type": "session.output_transcript.delta", "delta": "Ďakujem, dovidenia."})

    run(go())
    assert call.watchdog_action(clock.t + 1.0) is None  # still (maybe) speaking
    assert call.watchdog_action(clock.t + 3.0) == "hangup"


def test_speech_before_the_goodbye_was_injected_does_not_count():
    """Something she was still saying when end_call arrived is not the goodbye."""
    call, _, clock, _ = _live_call()

    async def go():
        await call.handle(_function_call("end_call", {}))
        clock.t += 0.2
        await call.handle({"type": "session.output_transcript.delta", "delta": "Dobre."})
        clock.t += 0.5
        await call.handle({"type": "session.instructions.appended", "client_event_id": "farewell"})

    run(go())
    assert call.watchdog_action(clock.t + 4.0) is None


def test_hangs_up_anyway_when_the_goodbye_never_comes():
    call, _, clock, _ = _live_call()
    run(call.handle(_function_call("end_call", {})))
    assert call.watchdog_action(clock.t + 5) is None
    assert call.watchdog_action(clock.t + 13) == "hangup_timeout"


def test_backend_is_continued_if_its_response_never_finishes():
    call, _, clock, _ = _live_call()
    run(call.handle(_function_call("search_listings", {})))
    assert call.watchdog_action(clock.t + 1) is None
    assert call.watchdog_action(clock.t + 4) == "continue"


def test_silence_hang_up_counts_both_speakers():
    call, _, clock, _ = _live_call()
    clock.t += 90
    run(call.handle({"type": "session.input_transcript.delta", "delta": "haló"}))
    assert call.watchdog_action(clock.t + 50) is None
    assert call.watchdog_action(clock.t + 101) == "hangup_silence"


def test_session_closed_ends_the_loop():
    call, _, _, _ = _live_call()
    run(call.handle({"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 42}}))
    assert call.closed


# ── The whole call, over a fake sideband ─────────────────────────────────────


class FakeSideband:
    """Stands in for the attach WebSocket: replays server events, records
    what we send."""

    def __init__(self, events):
        self._events = [json.dumps(e) for e in events]
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def test_full_call_accepts_greets_runs_a_tool_and_tears_down(monkeypatch):
    events = [
        {"type": "session.output_transcript.delta", "delta": "Dobrý deň, volám sa Apollonia."},
        {"type": "session.input_transcript.delta", "delta": "Hľadám byt v Bratislave."},
        _function_call("search_listings", {"zone": "Bratislava"}),
        _completed(),
        {"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 30}},
    ]
    sideband = FakeSideband(events)
    accepted, attached, torn_down = [], [], []

    async def fake_accept(session_id, config):
        accepted.append((session_id, config["model"]))
        return True

    def fake_connect(url, **kwargs):
        attached.append(url)
        return sideband

    async def fake_email(session):
        torn_down.append("email")

    def fake_persist(session):
        torn_down.append("persist")

    monkeypatch.setattr(live, "_accept", fake_accept)
    monkeypatch.setattr(live.websockets, "connect", fake_connect)
    monkeypatch.setattr(router, "_send_lead_email", fake_email)
    monkeypatch.setattr(router, "_persist_call", fake_persist)

    ctx = _ctx()
    ctx.tenant_store = FakeStore([LISTING])
    session = router._new_call_session("sess_9", ctx)
    run(live.run_live_call(
        "sess_9", live.build_session_config(ctx), session, ctx.content, ctx.tenant_store
    ))

    assert accepted == [("sess_9", "gpt-live-1")]
    assert attached == ["wss://api.openai.com/v1/live/sessions/sess_9/attach"]
    assert [e["type"] for e in sideband.sent] == [
        "session.instructions.append",  # greeting
        "response.item.create",         # search result
        "response.create",              # continue the backend
    ]
    assert session["listings_shown"] == [LISTING]
    assert session["started_at"] and session["ended_at"]
    assert torn_down == ["email", "persist"]


def test_failed_accept_sends_no_lead(monkeypatch):
    torn_down = []

    async def fake_accept(session_id, config):
        return False

    async def fake_email(session):
        torn_down.append("email")

    monkeypatch.setattr(live, "_accept", fake_accept)
    monkeypatch.setattr(router, "_send_lead_email", fake_email)
    ctx = _ctx()
    run(live.run_live_call("s", {}, router._new_call_session("s", ctx), ctx.content, None))
    assert torn_down == []


# ── Regressions from the first live calls (2026-09-22) ───────────────────────


def test_goodbye_without_an_acknowledgment_still_hangs_up():
    """First live call: she spoke after end_call, the farewell acknowledgment
    never preceded it, and the line stayed open until the caller hung up."""
    call, _, clock, _ = _live_call()

    async def go():
        await call.handle(_function_call("end_call", {}))
        clock.t += 1.6
        await call.handle({"type": "session.output_transcript.delta", "delta": "Ďakujem, dovidenia."})

    run(go())
    spoke_at = clock.t
    assert call.watchdog_action(spoke_at + 3.5) is None  # too soon after end_call
    assert call.watchdog_action(1000.0 + 6.0) == "hangup"


def test_no_announcement_while_waiting_for_the_end_of_the_call():
    """She said 'Dobre, končím hovor.' — the waiting-sentence allowance must
    not extend to ending the call."""
    text = live.build_voice_instructions(_ctx())
    assert "ukončení hovoru nepovedzte nič" in " ".join(text.split())
    assert "'končím hovor'" in text


def test_backend_writes_house_numbers_as_digits():
    """'Pezinská devätnásť' matched nothing; the store matches digits."""
    assert "'Pezinská 19'" in live.build_backend_instructions(_ctx())


def test_she_does_not_insist_on_a_street_the_caller_does_not_know():
    text = " ".join(live.build_voice_instructions(_ctx()).split())
    assert "Spýtajte sa iba raz. Ak adresu ani ulicu nevie" in text


def test_transcript_lines_carry_the_session_timeline(caplog):
    call, _, clock, _ = _live_call()
    caplog.set_level("INFO", logger="call.live")

    async def go():
        await call.handle({"type": "session.output_transcript.delta", "delta": "Dobrý ", "start_ms": 5200})
        await call.handle({"type": "session.output_transcript.delta", "delta": "deň.", "start_ms": 5400})
        await call.handle({"type": "session.input_transcript.delta", "delta": "Haló", "start_ms": 9000})
        call.transcript.flush()

    run(go())
    lines = [r.getMessage() for r in caplog.records]
    assert "Apollonia [t=5.2s]: Dobrý deň." in lines
    assert "Caller [t=9.0s]: Haló" in lines


# ── Greeting (she stayed silent until the caller said "Haló") ────────────────


def test_greeting_cue_is_an_order_to_speak_now_with_the_disclosure():
    greeting, retry = live.build_greetings(_ctx())
    for text in (greeting, retry):
        flat = " ".join(text.split())
        assert "HNEĎ TERAZ" in flat
        assert "volám sa Apollonia, som virtuálna asistentka Štúdio Demo Live" in flat
    # A scene description ("the phone rang") is what she waited through.
    assert "zazvonil" not in greeting


def test_greeting_cues_are_slovak_only():
    for text in live.build_greetings(_ctx()):
        assert not [w for w in _FOREIGN if w in text.lower()]


def _greeting_call():
    sent = []

    async def send(event):
        sent.append(event)

    clock = Clock()
    ctx = _ctx()
    call = live.LiveCall(
        "s", router._new_call_session("s", ctx), ctx.content, FakeStore([]), send, clock,
        greetings=live.build_greetings(ctx),
    )
    return call, sent, clock


def test_the_built_greeting_is_what_gets_sent():
    call, sent, _ = _greeting_call()
    run(call.start())
    assert sent[0]["content"] == call.greeting


def test_greeting_is_retried_once_if_she_stays_silent_after_it_lands():
    call, sent, clock = _greeting_call()

    async def go():
        await call.start()
        await call.handle({"type": "session.instructions.appended", "client_event_id": "greeting"})
        clock.t += 3.5
        assert call.watchdog_action(clock.t) == "greet"
        await call.retry_greeting()

    run(go())
    assert sent[-1]["event_id"] == "greeting_retry"
    assert sent[-1]["content"] == call.greeting_retry
    assert call.watchdog_action(clock.t + 10) is None  # only once


def test_no_retry_before_the_cue_has_even_landed():
    """Before the media is up the timeline has not started; retrying then would
    queue a second greeting behind the first."""
    call, _, clock = _greeting_call()
    run(call.start())
    assert call.watchdog_action(clock.t + 20) is None


def test_no_retry_once_she_has_greeted_or_the_caller_spoke():
    call, _, clock = _greeting_call()

    async def go():
        await call.handle({"type": "session.instructions.appended", "client_event_id": "greeting"})
        await call.handle({"type": "session.output_transcript.delta", "delta": "Dobrý deň"})

    run(go())
    assert call.watchdog_action(clock.t + 4) is None

    call, _, clock = _greeting_call()

    async def go2():
        await call.handle({"type": "session.instructions.appended", "client_event_id": "greeting"})
        await call.handle({"type": "session.input_transcript.delta", "delta": "Haló"})

    run(go2())
    assert call.watchdog_action(clock.t + 4) is None
