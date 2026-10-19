# ElevenLabs voice for Apollonia: roadmap

Status: **proposal, no code written** (2026-10-18)
Hard constraint: **the live production customer's call path must not change** until we decide to move them (Phase 6), and that move must be reversible in one step.
Second hard constraint: **the call recording assistant's Hetzner box is not touched.** Apollonia gets its own VM, its own Asterisk and its own DIDWW trunk; nothing is installed, reconfigured or restarted on the call assistant's box.

> **Changes after review (2026-10-18).** The first version assumed the Hetzner Asterisk box carried no other production traffic. It does: the call recording assistant runs on it (Groundwire/Acrobits softphones, recording, its own DIDWW trunks). The review led to a shared-box plan.
>
> **Update after Phase −1 (2026-10-18).** The inspection showed the shared box is too small for a second product: 1 vCPU, 927 MB RAM, no swap. Its Asterisk config is also baked into the call assistant's Docker image, so every Asterisk change would mean rebuilding and restarting that container. So:
> - **D1 decided:** from Phase 2 onwards the agent and its own Asterisk run on a **separate small Hetzner VM**. The shared box is not used at all. Phase 5b, the shared-box operations rules, the CPU-sharing checks and the separate Linux user's protections against the call assistant are gone.
> - **Trunk identification by IP** on the own VM (§2.3, §2.5, §4.3). The `eleven-` Request-URI prefix, the `endpoint_identifier_order` change, the one-line fallback and the `${EXTEN:7}` strip are removed.
> - **New VM setup** (§2.4): 2 vCPU / 4 GB class; its own cloud firewall and ufw modelled on the call assistant's; Asterisk 22 in Docker built from the call assistant's Dockerfile as a template, **plus `func_curl`** (§2.5).
> - **DIDWW failover made precise** (§2.3, §4.3, Phase 4, §6). Failover only happens on the selected Re-routing Disconnect Codes (408/no response, 503, 500/502/504), ringing timeout or capacity limit. 486 and 603 stay unselected so personal-number declines never fail over. The dialplan ends with `Hangup(34)` (503) when the Asterisk fallback to OpenAI fails.
>
> **Final review fixes (2026-10-18):**
> - **RTP open to any source** with `strictrtp=yes` (§2.4): fallback audio comes from OpenAI's media servers, not DIDWW. SRTP check for the `openai-sip` endpoint (§2.5).
> - **Dead-VM wait:** DIDWW confirmed how its timers work (§2.3). The ringing timeout only starts at an 18x response. **SIP Timer B** (default 8 s, adjustable 1–64 s) runs from the INVITE **until a final response** (200 or 4xx/5xx/6xx); provisional responses don't stop it. So Timer B must stay above the worst-case pre-answer time, and a dead VM costs callers about Timer B.
> - **One HTTP request before answer** (decided 2026-10-18): the dialplan calls only the local agent, which asks Render once (personal-number check + tenant engine together) and answers in plain text: `decline`, `realtime` or `ok:<uuid>`. The agent creates the AudioSocket UUID; `SHELL(uuidgen)` and the dialplan's separate `/noanswer-check` call are removed (§4.1, §4.2, §4.3).
> - **`CURL` timeout** (1 s connect, 2 s total): a timeout means fallback to OpenAI SIP, where today's code still declines personal numbers. Written worst-case pre-answer budget **~5.3 s**, fallback Dial reduced to 3 s, **Timer B 6000 ms** (§2.3, §4.3). Phase 4 tests the worst case.
> - **Backups** from Phase 3 on (§2.4).
>
> **VM OS and Python (decided 2026-10-19):** the VM `apollonia-voice` (nbg1) runs **Ubuntu 26.04.1 LTS** and is kept. The agent runs on its **system Python 3.14** in a venv at `/opt/apollonia-agent/venv` (§2.4, §4.2; [vm-setup-runbook.md](vm-setup-runbook.md) §3, §7.1, §8.3). The Asterisk image is unaffected (built `FROM debian:trixie` in Docker).
>
> **VM built (2026-10-19), runbook first pass done:** `apollonia-voice`, **91.99.238.181**, nbg1, Ubuntu 26.04.1, Python 3.14.4. The deployed Asterisk files are in the repo under [`voiceagent/pbx/`](voiceagent/pbx/) and are the source of truth ([vm-setup-runbook.md](vm-setup-runbook.md) §4–§6). What differed from the plan:
> - **Image:** the call assistant's Dockerfile was adapted. Debian trixie, Asterisk **22.11.0** with a pinned SHA-256, runs as non-root **`asterisk` (UID 10002)**, `func_curl` added, AudioSocket / `res_pjsip` / `res_srtp` / `func_curl` checked at build time, call-assistant extras removed; tag `apollonia-pbx:22`. Config is mounted **read-only** (§2.5).
> - **OpenAI endpoint:** `media_encryption=sdes`, matching the existing DIDWW → OpenAI trunk (TLS 5061 + SRTP SDES), which settles the SRTP check. Also `allow_wildcard_certs=yes`, because OpenAI's cert (CN=api.openai.com, SAN `*.api.openai.com`) was otherwise rejected with "server identity does not match" (§2.5).
> - **Dialplan:** the `;` in `[add-diversion]` must be escaped as `\;`. Otherwise no Diversion header is sent and OpenAI / Render answer 404 (§4.3).
> - **DIDWW:** trunk **`apollonia-eleven`** (UDP, IPv4 only, SRTP off, PCMA/PCMU) with only test DID **421482304191**. The 7 DIDWW Static Endpoint IPv4 addresses are in the cloud firewall, ufw and the pjsip identify. DIDWW sends From/To without "+" and no Diversion on direct calls (§2.3).
> - **Tests passed:** INVITE matched (from DIDWW Frankfurt); fallback to OpenAI with `marin`, two-way audio and the tenant found from Diversion; personal-number decline → 486 end to end.
> - **Open item before the trunk group goes live:** DIDWW doesn't re-route on **503** by default, and it couldn't be selected at trunk creation (§2.3, Phase 4).

---

## 1. Goal and why this shape

Give Apollonia an ElevenLabs voice (outstanding Slovak, and we keep Italian) while keeping what works today: gpt-realtime **hears the caller's audio directly**. We don't add a separate speech-to-text step.

### Why we can't do it inside today's setup
Today calls go DIDWW → **OpenAI SIP connector**, and OpenAI owns the audio. Our control WebSocket can drive tools, responses and hangup, but the Realtime API has no event that pushes our own audio to the caller. Switching the model to `output_modalities: ["text"]` would make the line silent. A "speak" tool can't help either, because a tool result goes back to the model as text and never reaches the caller.

### Options that were rejected
| Option | Why not |
|---|---|
| Text-only output + ElevenLabs over the existing OpenAI SIP connection | Impossible: no way to put audio into the call |
| ElevenLabs Agents platform (their SIP, STT, LLM, TTS) | It transcribes the caller with a separate speech-to-text step. We already measured that on Slovak phone audio: a separate transcription was *worse* than gpt-realtime's own understanding, especially on town names and numbers. It would also mean a full rewrite. |
| LiveKit Cloud | Works, but adds per-minute fees and a new vendor when we already operate Asterisk on Hetzner |
| Running audio on Render | Disk-backed service, so deploys stop the instance and drop calls. Audio would also share CPU with the video pipeline. |
| Sharing the call recording assistant's Asterisk box | Too small (1 vCPU / 1 GB, no swap), and its config is baked into that product's Docker image, so every change would rebuild and restart it (see Phase −1) |

### Chosen design
```
                        ┌──────── Hetzner: Apollonia's own VM ─────────────┐
Caller ─ PSTN ─ DIDWW ──┤ Asterisk ──AudioSocket (TCP, loopback)── Agent   │
          (demo DID     │ (Docker)                            │      │     │
           only, new    │   dialplan:                         │      │     │
           trunk in a   │   • personal-numbers check (486)    │      │     │
           trunk group) │   • register call with agent        │      │     │
                        │   • fallback → OpenAI SIP           │      │     │
                        └─────────────────────────────────────┼──────┼─────┘
                                                              │      │
                         gpt-realtime WS  ◄── caller audio ───┘      │
                         (audio in, TEXT out) ── text deltas ─► ElevenLabs WS
                                                                (v4 Turbo, pcm_8000)
                                                                     │
                                                 audio back to Asterisk ◄┘

Agent ── HTTPS + shared secret ──► Render API (/internal/voice/*)
         call setup, tools, end-of-call (leads, WhatsApp, usage)

VM unreachable / 503 ──► DIDWW trunk group fails over ──► OpenAI SIP trunk (marin)
```

All the business logic stays on Render: tenant resolution, prompts, locales, tool schemas, tool handlers, lead summary, e-mail and WhatsApp alerts, usage. The Hetzner agent only moves audio and turns, and calls Render for everything else.

The production customer's DID keeps pointing at the OpenAI SIP connector and keeps using today's code, unchanged.

The call recording assistant's box is a separate machine and plays no part in this design.

---

## 2. Accounts and things to set up

Tick these off as you go. Items marked **(P0)** are needed for the Phase 0 offline test; everything else can wait until its phase.

### 2.1 ElevenLabs
- [ ] **Plan with API access** that covers the expected volume **(P0)**
  - Bills by character. Rough check: one call minute of her speaking ≈ 600–900 characters. Estimate from real transcripts in `calls`.
  - **Concurrency limit: 6 on the current plan.** It counts requests *while audio is being generated*, not whole calls. So the agent opens a TTS stream **per spoken turn** and closes it when the turn is done (never one stream held for the whole call). She speaks roughly 40–50% of a call, so 6 slots should cover about 10–12 simultaneous calls before turns start colliding. When the limit is hit (HTTP 429 / concurrency error), the agent waits briefly and retries (a few hundred ms of extra silence) instead of failing the turn. Log every such wait so we know when to upgrade.
- [ ] **API key** dedicated to telephony, restricted to text-to-speech / text-to-dialogue only if the dashboard allows it. Don't reuse the key from your content work. **(P0)**
- [ ] **Voice ID(s)** **(P0)**
  - Slovak voice (the one you use for content), and an Italian voice if Italian tenants will use this.
  - **Licensing:** if the voice comes from the Voice Library, confirm commercial and telephony use is allowed for that voice. A voice you cloned yourself (PVC) is the safe option.
  - **Model: Eleven v4 Turbo** (`eleven_v4_turbo`), confirmed by ElevenLabs support:
    - it runs on the **Text-to-Dialogue WebSocket** `wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input`. The standard Text-to-Speech WebSocket does **not** support `eleven_v4` / `eleven_v4_turbo`;
    - **`pcm_8000`** is a supported format;
    - alignment timestamps come with `sync_alignment=true` (§4.2 step 4).
  - Still to check in Phase 0: Slovak (and Italian) quality, and that `output_format=pcm_8000` works on this exact WebSocket. Support confirmed it "in conversational configurations"; a first connect settles it.
  - Your content may be made with a different model, and the same voice can sound different on another model, so Phase 0 must judge **v4 Turbo at 8 kHz**, not your content output.
- [ ] **Pronunciation dictionary** (alias rules) for Slovak town and street names, and brand words like "ApollonIA". Note its ID and version. This replaces the prompt respellings we reverted. **Being created 2026-10-19**, first rule `Dovidenia → Do videnia` (plus lowercase); ID and version go in the VM's env file.
- [ ] **Usage alert / spending cap** in the dashboard.
- [ ] **Privacy:** read the ElevenLabs DPA. Check zero-retention or EU data residency options; these are usually Enterprise-only, so decide whether that matters for your customers. ElevenLabs becomes a **new subprocessor**: it receives her text, not the caller's voice.

### 2.2 OpenAI
- [ ] Nothing new is required: the existing project and key keep working, and **the SIP connector and webhook stay configured exactly as now** (production depends on them).
- [ ] *Recommended:* a **separate project or API key for the Hetzner agent**, so its usage and rate limits show separately and the key can be rotated without touching Render.
- [ ] Usage alert on that project.

### 2.3 DIDWW
- [x] **New Voice IN trunk** (SIP) pointing to **Apollonia's own VM** public IP (§2.4). **Do not edit the existing OpenAI trunk, or the call recording assistant's inbound and outbound trunks.**
  - **Created 2026-10-19: `apollonia-eleven`** → 91.99.238.181:5060, **UDP, IPv4 only, SRTP disabled**, codecs **PCMA, PCMU**. Only test DID **421482304191** is on it.
  - **What DIDWW sends** (seen in runbook §9.2): **From and To without "+"**, and **no Diversion header on direct calls**. So the dialplan's fallback adds the DID as Diversion (§4.3), and Render's router accepts it without "+" (it compares the last 9 digits).
  - Codec: G.711 A-law (PCMA) first, μ-law second.
  - **Identification:** by IP. The VM only receives this one trunk, so Asterisk matches DIDWW's signalling IPs to the Eleven endpoint (§2.5). The Request-URI user stays the default (the DID).
  - Make sure caller ID (From) and the **Diversion** header are passed through. Forwarded calls depend on Diversion today.
  - **Re-routing Disconnect Codes** on this trunk (DIDWW only fails over on the codes selected here, plus ringing timeout and capacity limit; 486, 401, 503 and 603 are not selected by default):
    - **select:** 408 (or "no response"), **503**, and 500 / 502 / 504 if offered;
    - **keep unselected:** **486** and **603**, so personal-number declines (`Hangup(17)` → 486) are never re-routed to the OpenAI trunk and answered there.
    - **OPEN ITEM (found 2026-10-19):** `apollonia-eleven` was created with DIDWW's **default** codes, and **503 couldn't be selected at trunk creation**. Until this is fixed, the dialplan's `Hangup(34)` → 503 would **not** make DIDWW re-route to the OpenAI trunk. **Before the trunk group goes live (Phase 4)**, do one of these and retest:
      1. **select 503**: on the trunk edit page, in the trunk group settings, or through DIDWW support;
      2. **change `Hangup(34)`** in the dialplan to a hangup cause that maps to a SIP code DIDWW re-routes **by default**, never 486 or 603.
    - Also check the **Ringing Timeout (5 s)** and **Timer B (6000 ms)** below on the trunk at the same time.
  - **DIDWW's two timers** (confirmed by DIDWW support, 2026-10-18):
    - **Ringing Timeout** (1–32 s): how long DIDWW waits for 200 OK **after an 18x** (180 Ringing / 183 Session Progress).
      - Our Asterisk sends no 18x of its own, so on the normal path this timer never starts.
      - It only starts during the fallback `Dial` to OpenAI SIP, if Asterisk passes on OpenAI's 180/183.
      - **Set it to 5 s**, longer than the 3 s fallback Dial timeout. Then the dialplan's own `Hangup(34)` (503) always comes first, and DIDWW re-routes on that.
    - **SIP Timer B** (INVITE transaction timeout, default **8000 ms**, adjustable 1000–64000 ms): runs from the initial INVITE **until a final response** (200 OK, or a 4xx/5xx/6xx). Provisional responses (100 Trying, 18x) **don't stop or reset it** (confirmed by DIDWW).
  - **Consequences:**
    - **Every pre-answer path must end in a final response before Timer B expires**, or DIDWW cancels and re-routes on its own. Timer B must stay above the worst-case pre-answer budget (**~5.3 s**, §4.3) with some margin: **set 6000 ms** on the Eleven trunk.
    - **Dead VM:** nothing answers, so the caller waits the full Timer B (**~6 s**) before DIDWW re-routes to the OpenAI trunk.
    - Rule for any later change: **Timer B ≥ worst-case budget + ~0.5 s**. If Phase 4 shows the fallback Dial needs more than 3 s, raise Timer B by the same amount (or cut elsewhere). If the budget is cut, Timer B can come down with it, which shortens the dead-VM wait too.
- [ ] Move **only the demo DID** (Slovak demo) to the new trunk in Phase 3.
- [x] DIDWW signalling IPs: the **7 Static Endpoint IPv4 addresses** from DIDWW's SIP connectivity reference (updated 14 Oct 2026), now in the Hetzner firewall, ufw and the pjsip identify (list in [vm-setup-runbook.md](vm-setup-runbook.md) §1). If DIDWW changes the list, update all three places.
- [ ] **Required by the end of Phase 5:** a **DIDWW trunk group with failover** for the demo DID: Eleven trunk first, the existing OpenAI SIP trunk second. This covers what the Asterisk fallback can't: the VM or Asterisk itself being down, or the Asterisk fallback Dial failing (503, §4.3). Adding the OpenAI trunk to a group must not change its settings. Check this in the DIDWW panel, and if it would, create a second identical OpenAI trunk for the group instead. Phase 6 uses the same group setup for the customer's DID.

### 2.4 Hetzner
- [ ] **New cloud VM for Apollonia** (D1 decided, §7), used from Phase 2 onwards:
  - **2 vCPU / 4 GB class**, for example CX22 (shared) or CPX21, **Ubuntu 26.04**, **same location** as today's box, roughly €5–10 a month. **Created 2026-10-19:** `apollonia-voice`, **91.99.238.181**, nbg1, Ubuntu 26.04.1 LTS, system Python 3.14.4. Known and harmless: `cloud-init-main` fails on every boot (it looks for `enp1s0`; the interface is `eth0`, and netplan matches by MAC), so cloud-init stays enabled. If Phase 4's load test stutters, move to a dedicated CCX13.
  - Docker installed (Asterisk runs in a container, §2.5); the agent runs on the host (§4.2).
  - **Backups from Phase 3 on**, once the demo DID depends on the VM: turn on Hetzner automatic backups (about 20% of the server price), or at least take a manual snapshot before every Asterisk config change or image rebuild. Not needed while the box is fresh (Phases 1–2).
- [ ] **Firewall: its own Hetzner cloud firewall plus ufw**, modelled on the call assistant's box:
  - **5060/udp: DIDWW signalling IPs only.**
  - **RTP 10000–10999/udp open to any source**, as on the call assistant's box, with **`strictrtp=yes`** in `rtp.conf` (the same port range goes there too). **Don't limit it to DIDWW IPs:** on fallback calls (§4.3) the OpenAI side's audio arrives from **OpenAI's media servers**, so a DIDWW-only rule would connect those calls with no audio from Apollonia.
  - **SSH:** your IPs and key only.
  - **No softphone rules** and no inbound 5061: nothing registers to this Asterisk.
  - AudioSocket (9092) and the agent's local HTTP port (9093): **loopback only**, never public.
  - Outbound: HTTPS to `api.openai.com`, `api.elevenlabs.io` and the Render API; SIP-TLS 5061 to `sip.api.openai.com` (fallback).
  - **fail2ban** jails for `asterisk` and `sshd`, as on the call assistant's box. Running on the VM: `sshd` with `backend = systemd`, `asterisk` reading `messages.log` (written by Asterisk's UID 10002).
- [ ] **Agent runs as its own Linux user** (for example `apollonia-agent`, no login shell), with its own env file `/etc/apollonia-agent.env` owned by that user, mode `0600`. Asterisk reaches the agent over loopback TCP (the container uses host networking), so no shared group is needed.
- [ ] *Optional:* DNS name such as `pbx.apollon-ia.com` → VM IP, if you want SIP-TLS from DIDWW (Let's Encrypt cert).
- [ ] **The call recording assistant's box: not touched.** No packages, firewall rules, containers or config changes.

### 2.5 Asterisk (on the new VM)
**Built 2026-10-19.** Source of truth: [`voiceagent/pbx/`](voiceagent/pbx/) (`Dockerfile`, `compose.yaml`, `etc/*.conf`), deployed to `/opt/apollonia-pbx` on the VM.
- [x] **Asterisk 22 in Docker, host networking**, built from the call assistant's Dockerfile in `/opt/pbx` (read-only template) and **adapted** in [`voiceagent/pbx/Dockerfile`](voiceagent/pbx/Dockerfile):
  - **Debian trixie** (pinned image), **Asterisk 22.11.0** downloaded and checked against a **pinned SHA-256**;
  - **`func_curl` added** (libcurl dev package at build time). It isn't in the call assistant's image, and the dialplan needs it (§4.3);
  - the build **fails** if `res_pjsip`, `res_srtp`, `res_resolver_unbound`, `func_curl`, `res_audiosocket` or `chan_audiosocket` is missing;
  - runs as **non-root user `asterisk`, UID 10002**. The mounted `logs` directory must be owned by 10002;
  - recording notice, sox/espeak, python3, the PBX scripts and the entrypoint removed;
  - image tag **`apollonia-pbx:22`**.
- [x] **Config mounted read-only** (`./etc:/etc/asterisk:ro`): `pjsip.conf`, `extensions.conf`, `rtp.conf`, `logger.conf`, `modules.conf`. Dialplan and PJSIP changes are made in the repo, copied over, then `dialplan reload` / `pjsip reload`, not a rebuild and restart.
- [x] After the first start, verify:
  ```
  asterisk -rx "core show version"            # 22.x
  asterisk -rx "module show like audiosocket" # chan_audiosocket + res_audiosocket
  asterisk -rx "module show like curl"        # func_curl
  asterisk -rx "pjsip show identifies"        # DIDWW IPs → eleven endpoint
  ```
- [x] New **PJSIP endpoint** for the Eleven trunk with **`identify` by DIDWW signalling IPs** (the 7 Static Endpoint IPv4 addresses) and `context=from-didww-eleven`. Default `endpoint_identifier_order` (`ip,username`) is fine; nothing else connects to this Asterisk.
- [x] New **PJSIP endpoint for OpenAI SIP** (the fallback): TLS transport to `sip.api.openai.com:5061`, URI `sip:<OPENAI_PROJECT_ID>@sip.api.openai.com;transport=tls`, plus the system CA bundle for TLS verification.
  - **SRTP: settled, `media_encryption=sdes`.** The existing DIDWW → OpenAI trunk uses TLS on 5061 + SRTP (SDES), and the fallback test had two-way audio with it.
  - **`allow_wildcard_certs=yes`** on the TLS transport, with **`verify_server=yes` kept**. OpenAI's certificate has CN=api.openai.com and SAN `*.api.openai.com`; without this option PJSIP rejected it with "server identity does not match".
- [ ] Ports 9092/9093/8088/5038: free on the call assistant's box (Phase −1). On the new VM they are free by construction, so check once with `ss -tlnup` after setup.

### 2.6 Render
- [ ] New env var: `VOICE_AGENT_TOKEN` (long random shared secret; the same value goes on Hetzner).
- [ ] *Optional:* `ELEVEN_DEMO_NUMBER_SK`, if the demo tenant should be created and pinned to `voice_engine='eleven'` at startup, the way `LIVE_DEMO_NUMBER_SK` does for GPT-Live.
- [ ] Deploys keep going through the **nightly scheduled deploy** (`.github/workflows/scheduled-deploy.yml`). No manual daytime deploys.

### 2.7 Monitoring and operations
- [ ] Uptime monitor on the agent's health endpoint (through an authenticated tunnel or a tiny public `/healthz` that reveals nothing). UptimeRobot / Better Stack free tier is enough.
- [ ] Alert target: e-mail, plus WhatsApp or Telegram for "agent down".
- [ ] Logs: journald + logrotate on the VM. Never log raw audio.
- [ ] Shared secrets stored in a password manager: ElevenLabs key, OpenAI agent key, `VOICE_AGENT_TOKEN`, DIDWW credentials.
- [ ] **Restarts on the VM** (agent deploys, Asterisk container rebuilds) drop only active Apollonia calls. Do them at quiet hours, after checking `asterisk -rx "core show channels"`. The call recording assistant is on another box and unaffected.

### 2.8 Legal and customers
- [ ] Add **ElevenLabs** (TTS) and **Hetzner** (call audio passes through, nothing stored) to the subprocessor list in the privacy policy and T&C (`apollonia_termini_condizioni.*`) **before** any customer call uses this path.
- [ ] The production customer is informed, and agrees, **before** Phase 6.

---

## 3. Environment variables

### Hetzner agent (new service, `/etc/apollonia-agent.env`, owned by user `apollonia-agent`, mode `0600`)
| Var | Example / note |
|---|---|
| `OPENAI_API_KEY` | agent-specific key (2.2) |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1`: the same model as production, so prompts, tools and VAD tuning carry over. Only the output changes, to text. |
| `ELEVENLABS_API_KEY` | telephony key (2.1) |
| `ELEVENLABS_MODEL` | `eleven_v4_turbo` |
| `ELEVENLABS_WS_URL` | `wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input` (or the EU residency host) |
| `ELEVENLABS_VOICE_ID_SK` | Slovak voice: `WtA85syCrJwasGeHGH2p` |
| `ELEVENLABS_VOICE_ID_IT` | Italian voice (optional) |
| `ELEVENLABS_PRONUNCIATION_DICT_ID` / `_VERSION` | from 2.1 |
| `ELEVENLABS_LANGUAGE_CODE` | `sk` (ISO 639-1). `it` for Italian tenants later. Sent as the `language_code` query parameter on the Text-to-Dialogue stream. Supported by `eleven_v4_turbo` per ElevenLabs support; tested in Phase 0 against the drift into Czech |
| `ELEVENLABS_OUTPUT_FORMAT` | `pcm_8000` (goes straight into AudioSocket's 8 kHz audio). Format is `codec_samplerate[_bitrate]`; only `mp3_*_192` (Creator+) and `pcm_44100` (Pro+) are tier-restricted, so `pcm_8000` needs no particular plan. Fallback if this endpoint refuses it: `ulaw_8000`, which the agent decodes to 16-bit PCM with a 256-entry lookup table |
| `RENDER_API_BASE` | `https://<render-service>` |
| `VOICE_AGENT_TOKEN` | same as Render |
| `AUDIOSOCKET_BIND` | `127.0.0.1:9092` |
| `AGENT_HTTP_BIND` | `127.0.0.1:9093` (dialplan registers calls here) |

### Render (additions only)
| Var | Note |
|---|---|
| `VOICE_AGENT_TOKEN` | required for `/internal/voice/*`; endpoints return 404 when unset |
| `ELEVEN_DEMO_NUMBER_SK` | optional, see 2.6 |

---

## 4. Code work

### 4.1 Render, additive only (`backend/`)
Rule: **new code calls the existing functions; it does not restructure them.** If sharing code would require changing a function that production uses, copy the code instead and note the duplication.

- **`voice_engine='eleven'`:** the column already exists (`tenants/db.py`, default `'realtime'`). Accept the new value in the admin/tenant update path and make sure `_uses_live_engine` and the realtime handler ignore it. An `eleven` tenant's DID never reaches the OpenAI webhook except through the fallback, which must then be handled like `realtime`.
  - **Switching a tenant is a two-step change** (decided 2026-10-18): (1) point the tenant's DID at the Eleven trunk group in DIDWW, and (2) set `voice_engine='eleven'`. Both mismatches fall back to `marin`:
    - DID still on OpenAI with tenant set to `eleven`: handled like `realtime` (above).
    - DID on the Eleven trunk with tenant set to `realtime`: `POST /calls` returns `decision: "realtime"` and the dialplan sends the call to OpenAI SIP (§4.3).
- **New router `backend/call/agent_api.py`** mounted at `/internal/voice`, bearer-token protected by `VOICE_AGENT_TOKEN`, 404 when unset. Only the agent calls it; the dialplan never talks to Render directly.
  - `POST /calls` `{call_uuid, from, to, diversion}` → **one answer for everything decided before the call is picked up**, in this order:
    1. **personal-numbers check** (reuses the existing lookup): listed caller → `{decision: "decline"}`;
    2. **tenant engine:** tenant not on `eleven`, or unknown DID → `{decision: "realtime"}`, no session created;
    3. otherwise `{decision: "eleven", locale, session, greeting_prompt, farewell_response}`:
       - `session` is the **OpenAI SIP accept config, as is** (`_build_accept_config`): instructions, tools, `tool_choice`, reasoning and the tuned turn detection. The agent sets text output and its audio formats on top.
       - `farewell_response` is the ready-made `response.create` for the goodbye (`_farewell_response_event`).
       - **The voice ID stays on the VM:** the agent picks `ELEVENLABS_VOICE_ID_<LOCALE>` from `locale`.
    - It must answer fast: the agent waits at most 1.5 s (§4.2).
    - Builds a synthetic `sip_headers` list and passes it to the **unchanged** `_resolve_call`, then `_build_system_prompt` / `_build_accept_config` for instructions and tools. The prompt assembly is a **copy** of `_handle_realtime_incoming`'s few lines (`agent_api._instructions`): a prompt rule added there belongs there too.
    - Creates the server-side session with `_new_call_session`, kept in memory, keyed by `call_uuid`. A retried registration for the same `call_uuid` returns the same session.
  - `POST /calls/{uuid}/tool` `{name, arguments}` → `{output}`. Wraps `_execute_tool` with the stored session; `arguments` may be the model's JSON string or a parsed object. `output` is null for a name that isn't a data tool (`end_call` is the agent's own). Unknown call → 404.
  - `POST /calls/{uuid}/end` `{started_at, ended_at, ended_by, from, to, diversion, tool_calls[], transcript[]}` → **202 at once**. Then, in the background, the same end-of-call path as `_run_call`: `_send_lead_email` (summary, e-mail, WhatsApp), then `_persist_call` (call row, contact, minutes). A repeat `/end` for the same call is a no-op, so retries never send two leads.
    - **Restart safety:** the lead is built from what the **tools** put in the session, not from the transcript. So the agent sends `from/to/diversion` and **every data tool call in order** (`tool_calls`). If Render restarted mid-call and lost the session, `/end` resolves the call again and **replays the tool calls** to rebuild it, and the lead comes out the same. The `transcript` is only logged.
  - **Built 2026-10-19:** `backend/call/agent_api.py`, `VOICE_AGENT_TOKEN` in `config.py`, mounted in `main.py`. `POST /admin/tenants/{id}/settings` accepts `voice_engine` (`realtime` | `live` | `eleven`).
- **Tests** in `backend/tests/`:
  - auth required
  - unknown DID
  - personal-number caller → `{decision: "decline"}`, also for a tenant on `realtime`
  - tenant on `realtime` → `{decision: "realtime"}`, no session created
  - tool round-trip
  - end-of-call creates the lead and alerts
  - session-lost-then-end still persists
  - The full existing suite must stay green.
  - **Done:** `backend/tests/test_agent_api.py` (20 tests, these cases plus retries, Diversion routing, the 202/no-op `/end`, and "an `eleven` tenant reaching OpenAI SIP is answered on Realtime") and one admin test for `voice_engine`. Full suite **641 passed**.

### 4.2 Hetzner agent (new top-level folder `voiceagent/`, which Render ignores because its Docker context is `./backend`)
**Python 3.14** (the VM's system Python on Ubuntu 26.04) in a venv at `/opt/apollonia-agent/venv`, asyncio, systemd service on the VM host running as its own user `apollonia-agent` (§2.4). Check whether **Pipecat** already covers the realtime-text + ElevenLabs pipeline before writing it from scratch; either way these must exist:

> **Python 3.14 check, before choosing Pipecat or any other dependency:**
> - Confirm each dependency supports Python 3.14 with **published wheels**: `websockets`, `numpy`, and any audio or resampling library (for example `soxr`), plus Pipecat itself if chosen.
> - **`audioop` is gone** from the standard library since 3.13. Don't plan on it for resampling or μ-law; use a library (or the `audioop-lts` backport) and the 256-entry μ-law table (§3).
> - **If something important doesn't support 3.14,** the fallback is a **uv-managed Python 3.12 inside the same venv** (`/opt/apollonia-agent/venv`, so the systemd unit doesn't change), **not a different OS**.

1. **Call registry:** small HTTP server on loopback.
   - The dialplan posts `{from, to, diversion}`. The agent **creates the AudioSocket UUID**, calls Render `POST /calls` (JSON) with it, and keeps the setup ready for when the AudioSocket connects.
   - It answers the dialplan in **plain text**, mapped from Render's `decision`:
     - `decline` (personal number, answered with 486);
     - `realtime` (send to OpenAI SIP);
     - `ok:<uuid>` (take the call on AudioSocket).
   - Its own timeout for the Render call is **1.5 s**, so it can still answer within the dialplan's 2 s limit. If Render is slow or down, it answers `realtime`, and the call goes to the fallback, where today's OpenAI-side code still declines personal numbers.
2. **AudioSocket server:** TCP. Reads 8 kHz signed-linear 20 ms frames and writes audio frames back; sends hangup when the call ends.
3. **OpenAI realtime connection:**
   - `session.update` with Render's instructions and tools, `output_modalities: ["text"]`, the same `server_vad` settings as production (`_TURN_DETECTION`), `reasoning.effort: low`, no input transcription, no noise reduction (both settled decisions, see the comments in `router.py`).
   - Caller audio: 8 kHz → 24 kHz PCM resample, or send `audio/pcmu` directly. Pick whichever gives better comprehension in Phase 2.
4. **TTS:** stream `response.output_text.delta` into the ElevenLabs **Text-to-Dialogue** WebSocket (`/v1/text-to-dialogue/stream-input`, `model_id=eleven_v4_turbo`, `output_format=pcm_8000`, `sync_alignment=true`, pronunciation dictionary). **One TTS stream per response:** open it on `response.created` (or when the first text arrives) and close it when the response is done or interrupted, so a concurrency slot is held only while she is speaking. Opening early hides the TLS handshake; Phase 0 checks whether an open-but-idle stream already counts against the limit.
   **Text-to-Dialogue WebSocket** (decided: the only one that supports `eleven_v4_turbo`). How it works, from the API reference:
   | | |
   |---|---|
   | Auth | `xi-api-key` header |
   | First message | `voices: [<voice_id>]` (one voice: hers), optional `voice_settings`, and `pronunciation_dictionary_locators`. The last two are only accepted here, in the first message |
   | Text | `inputs: [{text, voice_id}]` as the deltas arrive |
   | Audio back | `audio` (base64 `pcm_8000`) + `alignment` with `chars`, `char_start_times_ms`, `char_durations_ms` (snake_case) |
   | Buffering | waits for ~40 characters / 8 words before producing audio, unless `flush: true` |
   | End of turn | `is_final_audio_for_turn` from the server; `close_socket: true` from us |
   | Idle | 20 s inactivity timeout; `keep_alive: true` resets it (not needed with one stream per response) |
   | Errors | `message`, `error`, `code`, `param` |
   - Send `flush` at the end of every sentence and at the end of the response, so short replies ("Áno, rozumiem.") don't wait for the buffer to fill.
   - **Alignment timing: measured in Phase 0, per chunk.** Support said `char_start_times_ms` counts from the start of the generated audio, but in practice it **restarts at 0 in every audio message**, and chunk boundaries vary between runs. The agent keeps a **running offset per reply** (add each message's last start + duration) and compares "ms of this reply already played" with offset + start to find the last character the caller heard.
   - **Spell out numbers before sending** (decided in Phase 0: digits read by the model drift into Czech; spelled out, they don't). The Phase 0 `sk_numbers.py` **moves into `voiceagent/`** as the agent's spell-out module: `spell_numbers()` and the streaming `SlovakNumberStream`, which holds back a number until it's complete across deltas. Phone numbers are read digit by digit, group by group; amounts and house numbers as Slovak numerals; times as "o štrnástej tridsať". Its self-tests become unit tests. Still to add: gender agreement ("jedna izba") and postal codes.
   - **`language_code`** from `ELEVENLABS_LANGUAGE_CODE` (`sk`) on every stream (decided in Phase 0, together with the spell-out).
   - **Pronunciation dictionary** from `ELEVENLABS_PRONUNCIATION_DICT_ID` / `_VERSION` in the first message of every stream (`pronunciation_dictionary_locators`). It holds **alias rules**; first rule: `Dovidenia → Do videnia`, plus lowercase.
   - **No early flush.** Flush only at sentence ends and at the end of the reply. A fixed-word-count early flush truncated audio and sounded choppy in Phase 0 (outcome 5).
   - **Keep the connection path warm:** the first stream of a process takes ~900 ms to connect versus ~90 ms after (Phase 0), so open a throwaway stream at agent startup.
   - **Treat a close with code 1000 as the normal end of a stream,** even when `websockets` reports it as `ConnectionClosedError`.
   - **`voice_settings` for `eleven_v4_turbo`** (per ElevenLabs support): `stability`, `speed` and `similarity_boost`. Set them in the first message and tune them in Phase 4. Support stated this "in conversational configurations", so Phase 0 checks that this WebSocket accepts all three.
   - The EU residency host (`wss://api.eu.residency.elevenlabs.io/...`) exists for this endpoint if the plan allows it.
   - Play the audio to AudioSocket at real-time pace using a small jitter buffer.
5. **Barge-in:** on `input_audio_buffer.speech_started`:
   - stop sending text, close or flush the TTS stream, clear the playback buffer;
   - use ElevenLabs **alignment timestamps** to work out exactly which characters the caller heard;
   - **replace the assistant's conversation item with only the spoken part** (`conversation.item.delete` + `conversation.item.create`). Otherwise she thinks she said things the caller never heard.
6. **Port the call-loop behaviour from `_run_call`:** `_ResponseGate` (one response at a time), reply nudge (`_should_nudge_reply`), silence check (`_should_check_caller_is_there`), farewell then hangup (`_farewell_response_event` + hangup after the goodbye has *finished playing*), and max-duration guard. The pure helper functions can be copied as they are. Write down which version of `router.py` they were copied from.
7. **Tools:** `response.function_call_arguments.done` → Render `POST /calls/{uuid}/tool` → `conversation.item.create` (function_call_output) + `response.create`.
8. **End of call:** on hangup from either side, call Render `POST /calls/{uuid}/end` with the timestamps, `from/to/diversion`, **every data tool call in order** (needed to rebuild the session after a Render restart, §4.1) and the transcript. Retry with backoff until it gets a 202; spool to disk under `/var/lib/apollonia-agent` if Render is unreachable, then retry.
9. **Greeting:** *optional improvement:* cache the pre-generated TTS greeting per tenant, so pickup is instant and the opening can't be cut off by the model regenerating it.
10. **Health:** `/healthz` (OpenAI and ElevenLabs reachable, Render reachable, AudioSocket listening).

### 4.3 Asterisk dialplan (Apollonia's own Asterisk)
Calls reach `[from-didww-eleven]` through the Eleven endpoint, identified by DIDWW's IPs (§2.5). `${EXTEN}` is the DID.

Sketch, not final:
```
[from-didww-eleven]
exten => _X.,1,NoOp(Eleven inbound ${CALLERID(num)} -> ${EXTEN})
 same => n,Set(DIV=${PJSIP_HEADER(read,Diversion)})
 ; ONE request, to the local agent (which asks Render once):
 ; plain text "decline" | "realtime" | "ok:<uuid>"; timeout or error → "" → fallback
 same => n,Set(CURLOPT(conntimeout)=1)
 same => n,Set(CURLOPT(httptimeout)=2)
 same => n,Set(REG=${CURL(http://127.0.0.1:9093/calls,<from/to/diversion>)})
 same => n,GotoIf($["${REG}" = "decline"]?busy)
 same => n,GotoIf($["${CUT(REG,:,1)}" != "ok"]?fallback)  ; "realtime", "" (timeout) or anything else
 same => n,Set(UUID=${CUT(REG,:,2)})                       ; UUID created by the agent
 same => n,Dial(AudioSocket/127.0.0.1:9092/${UUID})
 same => n,GotoIf($["${DIALSTATUS}" != "ANSWER"]?fallback) ; agent didn't take the call
 same => n,Hangup()
 same => n(busy),Hangup(17)                               ; → SIP 486, before answer. Never re-routed (§2.3)
 same => n(fallback),Dial(PJSIP/openai-sip,3,b(add-diversion^s^1(${EXTEN})))  ; Diversion = the DID
 same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?done)
 same => n,GotoIf($["${DIALSTATUS}" = "BUSY"]?busy)       ; OpenAI side declined (personal number) → 486, no re-route
 same => n,Hangup(34)                                     ; → SIP 503: DIDWW re-routes to the OpenAI trunk
 same => n(done),Hangup()
```
- **Timeouts:**
  - `CURLOPT(httptimeout)=2` caps the request at 2 s in total; `conntimeout=1` limits the connect phase within that.
  - A **timeout** (empty answer) means **fallback** to OpenAI SIP. Personal numbers stay protected even then, because today's OpenAI-side code declines them with 486, and the `BUSY` line below passes that on.
  - The request goes over loopback, so no Render token is stored in the dialplan; only the agent holds `VOICE_AGENT_TOKEN`.
- **Worst-case pre-answer budget** (agent or Render slow, so the request times out, then the OpenAI Dial fails):
  | Step | Worst case |
  |---|---|
  | agent request (times out → fallback) | 2.0 s |
  | fallback `Dial` to OpenAI SIP (timeout) | 3.0 s |
  | dialplan + SIP overhead | ~0.3 s |
  | **Total before `Hangup(34)`** | **~5.3 s** |
  
  This fits under DIDWW's timers (§2.3), with ~0.7 s margin:
  - **Timer B (6 s)**, which runs until the final response;
  - the **5 s Ringing Timeout**, which can only start during the 3 s fallback Dial.
  
  Any change to a timeout above must keep the total under Timer B.
  
  After `Hangup(34)`, DIDWW sends the call to the OpenAI trunk directly, so the caller waits about 6 s before `marin` answers. The normal path (request fast, agent answers) should be well under 1 s.
  
  If a DIDWW timer fires first anyway, DIDWW cancels the INVITE to Asterisk. Asterisk then hangs up its own leg to OpenAI, and DIDWW tries the OpenAI trunk itself, so the caller isn't connected twice.
- The fallback puts **the DID (`${EXTEN}`) into the Diversion header** and keeps **From** (the caller). On DIDWW calls today OpenAI sees its project ID in `To`, and `_resolve_call` finds the tenant from Diversion, so this matches what it gets now. In the fallback the call runs on today's realtime path, with the `marin` voice. Deployed config: [`voiceagent/pbx/etc/extensions.conf`](voiceagent/pbx/etc/extensions.conf) (runbook §6.3). **Escape note:** in `extensions.conf` a `;` starts a comment, so the header value is written `<sip:${ARG1}@91.99.238.181>\;reason=unconditional`. Without the backslash no Diversion is sent and OpenAI / Render answer 404. Tested 2026-10-19: tenant found from Diversion, `marin` answered with two-way audio.
- **Failure codes:**
  - If the Asterisk fallback Dial to OpenAI SIP fails, the call ends with **`Hangup(34)` → 503**. 503 is a selected Re-routing Disconnect Code, so DIDWW tries the OpenAI trunk directly.
  - **Never use 503 (or any selected code) for a decline:** the personal-number decline is `Hangup(17)` → 486, which is unselected and therefore final.
- Once the call is answered (by the agent or OpenAI), DIDWW can no longer re-route it. An agent failure *after* answer drops that call; only new calls benefit from failover.
- This Asterisk fallback covers **the agent** failing. If the VM or Asterisk itself is down, DIDWW gets no response (408 / no response) and the **trunk group failover** (§2.3) sends the call to the OpenAI trunk.

---

## 5. Phases

Every phase has an exit criterion and a rollback. **Phases 0–5 cannot affect the production customer.** None of them touch the call recording assistant's box.

### Phase −1: inspect the call assistant's box (read-only), done 2026-10-18
Run on the call recording assistant's Hetzner box to decide whether it could host Apollonia. Nothing was changed.
```
nproc; free -h; df -h; lscpu | grep -i 'model name'
asterisk -rx "core show version"
asterisk -rx "module show like audiosocket"
asterisk -rx "pjsip show endpoints"
asterisk -rx "pjsip show identifies"
asterisk -rx "core show channels count"
ss -tlnup
```
Plus the Hetzner cloud firewall, ufw, fail2ban and cron jobs.

| Check | Output / finding |
|---|---|
| CPU / RAM / disk | **1 vCPU, 927 MB RAM (607 MB free), no swap** |
| Asterisk version | **22.11**, in Docker (image `assistant-pbx:local`, host networking) |
| AudioSocket modules | loaded |
| `func_curl` | **not in the image** |
| `match_request_uri` | supported |
| `endpoint_identifier_order` | `ip,username` |
| Config | **baked into the call assistant's image**; only `lines.json`, certs, sounds, logs and recordings are mounted |
| Listening ports (9092 / 9093 / 8088 / 5038) | all free |
| Host firewall (ufw) | 5060/udp DIDWW IPs only; 5061/tcp and RTP 10000–10999 open |
| fail2ban | jails for `asterisk` and `sshd` |
| FreePBX | no |
| Heavy cron jobs | none |

- **Decision (D1):** not used. It is too small for a second real-time product (1 vCPU / 1 GB, no swap). And because its config is baked into the image, adding even a context or `func_curl` would mean rebuilding and restarting the call assistant's container. Apollonia gets its own VM from Phase 2 onwards. The box's Dockerfile (`/opt/pbx`) and firewall serve only as templates (§2.4, §2.5).

### Phase 0: offline voice and delay test (≈1 day): **DONE 2026-10-19**
- Script in the scratchpad (not in the repo):
  - gpt-realtime text output → ElevenLabs Text-to-Dialogue WebSocket, `eleven_v4_turbo`, `pcm_8000`, with your voice. The first connect confirms `pcm_8000` on this endpoint, plus how the alignment times count and that `stability`, `speed` and `similarity_boost` are accepted (§4.2 step 4);
  - also measures the cost of opening a new TTS stream per turn (handshake time), and whether an open stream that isn't generating yet counts toward the concurrency limit;
  - 20–30 real Slovak lines (and Italian if relevant): phone numbers, prices, street and town names, "Pezinok", "Pezinská";
  - saves a WAV of each line and prints the delay from "response requested" → first text, and first text → first audio byte.
- Compare by ear against `marin` recordings from real calls.
- **Exit:**
  - the voice is clearly better on phone-quality audio;
  - **extra delay ≤ ~300 ms** compared with native realtime audio, from an EU host;
  - the pronunciation dictionary fixes the problem words.
- **If it fails:** stop. Nothing was changed.
- **First run, 2026-10-19** (on the VM, `eleven_v4_turbo`, `pcm_8000`, voice `WtA85syCrJwasGeHGH2p`; script `phase0.py` + `sk_numbers.py`, kept outside the repo in `/root/phase0`):
  - **Voice:** most lines sound **better than `marin`**.
  - **Numbers drift into Czech.** On the phone-number, price and house-number lines (3–6) she switches to Czech while reading the digits.
    - Fix under test: **`--spell-numbers`** sends digits as Slovak words. Phone numbers are read digit by digit, group by group; prices and house numbers as normal numerals.
    - Second fix under test: **`--language sk`** (`language_code` query parameter, per ElevenLabs support and the API reference).
  - **Czech accent on some lines.** Line 17 (*"Kedy by vám vyhovovalo, aby vám maklér zavolal?"*) sounds Slovak with a Czech accent.
    - "Dovidenia" (line 20) is stressed on the wrong syllable; Slovak stresses the first, **DO**-vi-de-nia.
    - Under test: `--language sk`, spelling variants via `--text-override`, and the pronunciation dictionary.
    - **Dictionary rules:** alias rules work on every model. ElevenLabs documents **phoneme rules and inline IPA for `eleven_v4`** (also v3 and flash v2); it doesn't say whether that includes `eleven_v4_turbo`, so test it. IPA/CMU phonemes for non-English languages need v3/v4.
  - **Timing:** `first_audio` median **231 ms** (128–363 ms), within the ≤ 300 ms target at the median. `first_text` median 545 ms, but that's gpt-realtime's own time and exists today too.
    - **Cold start:** the first stream takes **~900 ms** to connect versus **~90 ms** warm. The agent should open one ElevenLabs connection at startup and keep the TLS/DNS path warm.
    - **Streaming overhead:** about **+140 ms** compared with `--tts-only`. This endpoint has **no chunk-length or auto-mode setting** (API reference: generation starts once it has ~40 characters / 8 words, or on `flush`). The only lever is flushing early, and that was rejected (see outcome 5 below).
  - **Alignment:** `char_start_times_ms` **restarts at 0 in every audio message**, and chunk boundaries differ between runs. This contradicts what support said. **The agent must keep a running offset** (sum of each message's last start + duration) to map "ms played" to "characters heard" (§4.2 step 4).
  - **Normal close:** the server closes some streams with code 1000, which `websockets` reports as `ConnectionClosedError` (~7/20 lines). A normal close counts as end of stream (fixed in the script, and needed in the agent).
- **Outcome (2026-10-19), after the follow-up runs on lines 1–6, 9, 17 and 20.** Phase 0's exit criteria are met: the voice is better, the delay is within target, and the problem words are fixed.
  1. **Numbers: fixed.** `spell-sk` (`language_code=sk` + spell-out) sounds best, and the drift into Czech is gone. **Both go into the agent** (§4.2): `language_code=sk` on the Text-to-Dialogue stream, and `sk_numbers.py` moved into `voiceagent/` as the spell-out module.
  2. **Line 17 accent:** slightly better with `language_code=sk`; **passable**. Recheck on real calls in **Phase 5**.
  3. **"Dovidenia": fixed.** "Do videnia" (two words) is stressed correctly. It goes into the ElevenLabs **pronunciation dictionary as an alias rule** `Dovidenia → Do videnia`, plus lowercase `dovidenia → do videnia`. Dictionary ID and version go in the VM's env file (`ELEVENLABS_PRONUNCIATION_DICT_ID` / `_VERSION`).
     - **Dictionary check:**
       - **TTS-only:** stress correct in **10/10** samples with `language_code=sk`, **with and without** the dictionary. `language_code=sk` alone already fixes it there.
       - **Streaming** (gpt-realtime → ElevenLabs) without the dictionary: **_/5** correct *(result to fill in)*.
     - **Decision:** keep the dictionary anyway, as the mechanism for future words. The `Dovidenia` alias stays in it as a safety net, which matters if streaming turns out to be less reliable than TTS-only.
  4. **Delay: within target.** Without early flush, `first_audio` median is **~230–240 ms** across runs, inside Phase 0's **≤ ~300 ms extra**. Cold start is ~900 ms on the first request versus ~90–100 ms warm, so **the agent warms the connection at startup**.
  5. **Early flush: rejected.** `--first-flush-words 3` cut line 9 off after "môžem" (1.6 s of audio instead of ~4 s), and lines 1 and 5 sounded choppy. **The agent does not flush early**; it flushes only at sentence ends and at the end of the reply.
     - **Open question:** was the truncation a bug in the Phase 0 script or the endpoint's behaviour?
     - It only matters if Phase 4 needs lower delay. If so, understand the truncation first, and any retry flushes at the **first clause boundary**, never after a fixed word count.

### Phase 1: Render internal API (≈2 days)
- Section 4.1. Tests green. Deploy through the nightly deploy.
- **Status 2026-10-19: code done, not deployed.** Additive only: new `call/agent_api.py`, `VOICE_AGENT_TOKEN` setting, one `include_router`, `voice_engine` in the admin settings. Full suite 641 passed. **Remaining:**
  1. merge to `main`;
  2. set `VOICE_AGENT_TOKEN` on Render (the same value as in the VM's env file) so it goes out with the **nightly** deploy, not a daytime one;
  3. the exit check below.
- **Exit check passed 2026-10-19:** from the VM, `POST /internal/voice/calls` for 421482304191 → `{"decision":"realtime"}`, HTTP 200 (merged and pushed as `9a70526`, token set on Render). `RENDER_API_BASE` must include the scheme (`https://www.apollon-ia.com`); without it, curl gets a 307 redirect and an empty body. Still to do: the next-day check of production calls.
- **Exit:** endpoints respond correctly with `curl` from the VM (`POST /internal/voice/calls` for test DID 421482304191 → `{"decision":"realtime"}` while the test tenant is on `realtime`); production calls unaffected (check the next day's calls in the dashboard).
- **Rollback:** revert commit, or unset `VOICE_AGENT_TOKEN` (endpoints go 404).

### Phase 2: new VM, Asterisk + agent, test number only (≈4–5 days)
- Sections 2.3–2.5, 4.2, 4.3: create the VM and firewall, build the Asterisk 22 image from the template with `func_curl`, mounted config, Eleven and OpenAI endpoints, and the agent service.
- First test without moving any DID: create the new DIDWW trunk and point a **spare test DID** at it, or dial the context from inside the container (`channel originate`).
- **Status 2026-10-19: infrastructure done** ([vm-setup-runbook.md](vm-setup-runbook.md) §2–§9): VM, firewall, Asterisk image and config, agent user/env/unit, trunk `apollonia-eleven` with test DID 421482304191 (test tenant, `realtime`). Passed:
  - INVITE from DIDWW matched to `didww-eleven`;
  - fallback to OpenAI (`marin`, two-way audio, tenant found from Diversion);
  - personal-number decline → 486 end to end.
  
  **Remaining for the Phase 2 exit:** the agent itself (§4.2) and the full end-to-end call.
- **Exit:** a full call works end to end: greeting, listing search, phone-number read-back, booking tool, hangup, lead e-mail + WhatsApp, row in `calls`, usage counted.
- **Rollback:** nothing in production depends on the VM yet. Stop the agent and container, or delete the VM.

### Phase 3: move the demo DID (≈1 day)
- DIDWW: demo DID → new trunk. Demo tenant → `voice_engine='eleven'`.
- **Exit:** 10+ real calls from different phones and networks without failure.
- **Rollback:** DIDWW, point the demo DID back at the OpenAI trunk (instant), and set the tenant back to `realtime`.

### Phase 4: tuning and failover tests (≈3–5 days, spread out)
- Barge-in sensitivity, VAD silence, greeting behaviour, voice settings (`stability`, `speed`, `similarity_boost`), dictionary entries, the call-end timing after goodbye.
- **Playback stutter under load:** 3–4 simultaneous test calls on the VM; listen for stutter or gaps. If it stutters, move to CCX (§2.4).
- **Pre-answer timing, including the worst case** (§4.3 budget). Measure INVITE → answer (or `Hangup(34)`) for:
  - the **normal path** (target: under 1 s);
  - the **Asterisk fallback path** (agent stopped);
  - the **worst case:** make the agent request time out (for example point the agent's `RENDER_API_BASE` at a non-routable address with a too-long agent timeout, or pause the agent process), plus a failing OpenAI Dial. Expect `Hangup(34)` at ~5.3 s, before DIDWW's 6 s Timer B, and then the OpenAI trunk answering.
- **OpenAI answer time on the fallback path:** OpenAI's SIP connector only answers after our webhook has accepted the call. Measure INVITE → 200 OK from OpenAI on healthy fallback calls (10+ calls).
  - If it often comes close to the **3 s fallback Dial timeout**, healthy fallbacks would end in 503 and a second attempt through the OpenAI trunk (a longer wait, but still answered).
  - In that case raise the Dial timeout and Timer B by the same amount, keeping Timer B ≥ budget + 0.5 s, and keep the Ringing Timeout above the Dial timeout (§2.3, §4.3).
- **First, close the 503 open item (§2.3):** 503 isn't in DIDWW's default Re-routing Disconnect Codes and couldn't be selected when `apollonia-eleven` was created. Either select 503 (trunk edit page, trunk group settings or DIDWW support), or change the dialplan's `Hangup(34)` to a cause that maps to a code DIDWW re-routes by default (never 486/603). Test 3 below then shows it works, and test 4 shows declines still don't re-route.
- Set up the **DIDWW trunk group** (§2.3) with the Re-routing Disconnect Codes, the 5 s Ringing Timeout and Timer B at 6000 ms, then test. This is safe now, because the VM is Apollonia's alone:
  1. **Agent down:** stop the agent service and call → the Asterisk fallback sends it to OpenAI SIP; answered by today's agent for the right tenant.
  2. **VM / Asterisk down:** stop the Asterisk container, or power off the VM, and call → DIDWW gets no response and re-routes; **answered through the OpenAI trunk**.
     - **Measure how long the caller waits** before the OpenAI trunk answers. Nothing answers, so **Timer B** decides: expect about **6 s**, then the OpenAI trunk answering.
     - Test both cases: a stopped container (the VM may answer with ICMP unreachable, which can fail faster) and a powered-off VM (pure silence).
     - Record the measured waits here.
  3. **Asterisk fallback fails:** temporarily break the `openai-sip` endpoint (for example a wrong project ID) with the agent stopped → `Hangup(34)` / 503 → DIDWW re-routes; answered through the OpenAI trunk.
  4. **Personal-number decline:** call from a number on the tenant's personal-numbers list → caller gets **486 (busy), the call is not answered** by either trunk, and DIDWW does **not** fail over. Test it twice:
     - normal path: the agent answers `decline`, and the dialplan answers 486;
     - with the agent stopped (request times out → fallback): OpenAI's side declines, the fallback Dial returns `BUSY`, and the dialplan answers 486, not 503.

### Phase 5: trial period (≥2 weeks)
- Demo traffic plus your own test calls. Track:
  - call success rate
  - average extra delay
  - barge-in errors (she repeats or skips things)
  - misheard numbers
  - cost per minute (OpenAI text + audio-in, plus ElevenLabs characters, compared with today's realtime audio output)
- **Exit:**
  - no agent crash for 7 days;
  - Asterisk fallback tested (agent down → OpenAI SIP);
  - **required: DIDWW trunk group failover in place and tested** (all four Phase 4 tests passed, including the 486 decline not failing over);
  - no stutter in the under-load test;
  - cost per minute known and acceptable;
  - legal items (2.8) done.

### Phase 6: production customer (optional, their agreement first)
- Pick a quiet time. DIDWW: customer DID → the trunk group (Eleven trunk first, OpenAI SIP second); tenant → `voice_engine='eleven'`.
- Watch the first day's calls live.
- **Rollback:** DIDWW, point the DID back at the OpenAI trunk (instant), and set the tenant back to `realtime`. Today's code path was never removed, so this restores exactly the old behaviour.

**Total effort:** about 2 weeks of work to reach Phase 3 (including 1–2 days for the new VM and the Asterisk image), plus the trial period.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| A bug in the Render change breaks production calls | Additive-only rule, full test suite, nightly deploy, next-day check of production calls |
| Agent outage | Asterisk fallback to OpenAI SIP; uptime alert; systemd auto-restart |
| VM or Asterisk outage (takes out the Asterisk fallback too) | **DIDWW trunk group failover** on 408 / no response after Timer B (Eleven trunk first, OpenAI SIP second), **required** by the end of Phase 5 and tested in Phase 4 by stopping the container and powering off the VM |
| Asterisk fallback Dial to OpenAI fails | Dialplan ends with `Hangup(34)` → 503, so DIDWW tries the OpenAI trunk directly. **Only works once 503 is a selected re-routing code** (open item, §2.3; not in DIDWW's defaults); fixed and tested in Phase 4 before the trunk group goes live |
| A personal-number decline fails over and Apollonia answers anyway | Decline is `Hangup(17)` → 486; **486 and 603 stay unselected** in the Eleven trunk's Re-routing Disconnect Codes; never use 503 for a decline; tested in Phase 4 |
| DIDWW re-routes while Asterisk is still setting up the call (double answer or lost call) | Timer B (runs until the final response) at 6 s, above the ~5.3 s worst-case budget; Ringing Timeout 5 s, longer than the 3 s fallback Dial; a single `CURL` request (1 s connect / 2 s total); worst case tested in Phase 4; a CANCEL from DIDWW also hangs up Asterisk's OpenAI leg |
| Slow or unreachable Render blocks the call before answer | One request with a 2 s `CURL` timeout; the agent's own Render timeout is 1.5 s; a timeout means fallback to OpenAI SIP, where personal numbers are still declined |
| Dead VM: callers wait ~6 s before failover (DIDWW waits the full Timer B) | Accepted. It can only shrink together with the pre-answer budget (Timer B ≥ budget + ~0.5 s, §2.3); wait measured in Phase 4 test 2 |
| Healthy fallback calls hit the 3 s Dial timeout (OpenAI answers only after our webhook accepts) | OpenAI answer time measured in Phase 4; rebalance the timeouts within Timer B if needed |
| Fallback calls connect with no audio from the OpenAI side | RTP open to any source with `strictrtp=yes` (§2.4); SRTP (`media_encryption=sdes`) checked on `openai-sip` (§2.5); Phase 2 fallback test |
| VM lost or broken by a config change | Hetzner backups (or a snapshot before each change) from Phase 3 on (§2.4); config files in the repo |
| Call recording assistant disturbed | Its box is not used or touched at all; separate VM, separate trunk, separate image |
| Own Asterisk image drifts from the template or misses modules | Image built from a copy of the call assistant's Dockerfile with `func_curl` added; module check after first start (§2.5); config mounted, so changes don't need rebuilds |
| Small VM too slow (stutter) | 2 vCPU / 4 GB class; Phase 4 load test with simultaneous calls; move to CCX if needed |
| v4 Turbo voice is worse than your content voice | Phase 0 tests exactly v4 Turbo at 8 kHz before any build |
| **Slovak drifts into Czech on numbers** (phone numbers, prices, house numbers; Phase 0) | **Resolved in Phase 0:** spell-out module in the agent (`sk_numbers.py` → `voiceagent/`, §4.2) **plus** `language_code=sk` on every stream; together the drift is gone. Still to handle: gender agreement ("jedna izba"), postal codes |
| Wrong stress on some words ("Dovidenia") | **Resolved for "Dovidenia" in Phase 0:** `language_code=sk` alone gave correct stress in 10/10 TTS-only samples; the alias rule `Dovidenia → Do videnia` (plus lowercase) stays in the pronunciation dictionary as a safety net (streaming result pending). New words go into the same dictionary as they come up (phoneme/IPA rules only if `eleven_v4_turbo` supports them) |
| Slight Czech accent on some sentences (line 17) | `language_code=sk` made it passable; recheck on real calls in Phase 5 |
| Early flush truncates audio (Phase 0: line 9 cut after "môžem"; choppy on lines 1 and 5) | The agent doesn't flush early. Open question (script bug or endpoint behaviour), only relevant if Phase 4 needs lower delay. Any retry flushes at the first clause boundary, never after a fixed word count, and only once the truncation is understood |
| Barge-in cuts at the wrong character | Alignment times restart at 0 per audio message (Phase 0): the agent keeps a running offset per reply |
| Barge-in feels worse than OpenAI's native one | Alignment-based truncation; Phase 4 tuning; stay on the demo DID until it's good |
| ElevenLabs concurrency limit (6) hit at peak | Slot held only while she is speaking (one stream per turn); short wait-and-retry on 429; log the waits; upgrade the plan when they appear regularly. On sustained errors, send *new* calls to the Asterisk OpenAI fallback |
| ElevenLabs outage mid-call | Agent detects it and plays a short pre-recorded apology, then ends the call. The lead is still sent (transcript so far). New calls use the fallback. |
| Render restarts mid-call | Tool calls retry for a few seconds; the end-of-call payload carries the full transcript, so the lead is never lost |
| Loop logic diverges from `router.py` | Copied helpers record their source version; any change to the call loop in `router.py` gets a "port to agent?" checklist item |
| GDPR / customer trust | Subprocessor list updated (ElevenLabs, Hetzner) and customer informed before Phase 6 |

---

## 7. Open decisions

- **D1: where the agent runs. Decided 2026-10-18 after Phase −1:** a **separate small Hetzner VM** (2 vCPU / 4 GB class, same location, roughly €5–10 a month) with its own Asterisk 22 in Docker, from Phase 2 onwards.
  - **Why not the call assistant's box:** 1 vCPU / 927 MB RAM with no swap is too small for a second real-time product. And since its config is baked into the `assistant-pbx` image, every Asterisk change for Apollonia would mean rebuilding and restarting the call assistant's container.
  - **Cost:** the VM fee, about 1–2 days of setup, and one more box to monitor and patch.
  - **Benefit:** the two products are fully isolated, so restarts, deploys, firewall changes and CPU load on one never reach the other's customers. It also allows plain IP identification of the trunk and failover tests that power off the box.
- **Decided: per-tenant voice with a two-step switch.** `voice_engine` per tenant; tenants not on `eleven` keep gpt-realtime-2.1 with `marin`. Moving a tenant means changing both the DIDWW routing and `voice_engine` (§4.1). Routing every DID through the trunk group so the tenant setting alone decides was considered and **not chosen**: it would put every tenant's calls, including the production customer's, through Hetzner.
- **D2: Italian:** ElevenLabs for Italian tenants too, or Slovak only at first?
- **D3: caller audio format to OpenAI:** 24 kHz resampled PCM vs `audio/pcmu`. Decide on comprehension in Phase 2.
- **D4: Pipecat vs. hand-written agent:** decide after a short look at Pipecat's OpenAI-realtime + ElevenLabs support.
- **D5: ElevenLabs plan:** depends on Phase 0 character estimates and expected concurrent calls.
- **D6: pricing:** does the new voice change what customers pay (premium tier)?
