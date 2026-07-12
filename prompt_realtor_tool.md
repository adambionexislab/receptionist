# Claude Code Prompt — "Acquisizione" seller-meeting capture module

> Paste everything below into Claude Code from the root of the dashboard repo.
> It is written so Claude Code **explores first, proposes a plan, and waits for your approval before writing any code** — this is the safest way to add a feature to an existing codebase without breaking it.

---

## ROLE & GROUND RULES

You are adding a new, self-contained feature to an **existing customer dashboard** (do not assume a greenfield project). Before writing any code:

1. **Explore the repo.** Identify: the web framework (backend + frontend), how routes/pages are registered, how auth/session works, how the current customer is identified per request, how data is persisted, how static/JS assets are served, how env vars and secrets are loaded, and the existing code style/conventions (naming, formatting, error handling).
2. **Produce a short written plan** — file tree of what you'll add, which existing files you'll touch (aim for as few as possible, ideally only a router registration + a nav entry), the data model, and the endpoints. **Then stop and wait for my approval.** Do not modify anything until I say "go".

**Hard constraints (do not violate):**
- **Additive only.** New feature lives in its own module/router/blueprint and its own frontend page/section. Do not refactor, rename, or "improve" unrelated existing code.
- **Reuse existing auth** — the feature must be behind the same login the rest of the dashboard uses, scoped to the logged-in customer. Do not invent a new auth path.
- **No destructive migrations.** If a DB is used, only additive schema changes (new tables). Provide the migration in whatever tool the repo already uses.
- **Match existing conventions** for everything (framework idioms, folder layout, style).
- **All secrets via existing env-var mechanism.** Never hardcode the OpenAI key.
- Keep it behind a **feature flag / config toggle** so it can be shipped dark.

**TWO MARKETS — IT & SK (important):** This dashboard serves ApollonIA in **both Italy and Slovakia from the same repo**. The feature must work in **Italian and Slovak**. Do **not** hardcode language anywhere. During Phase 0, find out how the app already knows which market/locale a customer belongs to (a `market`/`locale`/`country` field on the customer, a tenant setting, a UI language switch — whatever exists) and **reuse that**. If nothing exists, propose the minimal way to attach a `market ∈ {"it","sk"}` to each customer/record and let me approve it. A single `market` value must then drive, consistently through the whole flow: consent text language, the transcription language hint, the extraction prompt + required-fields schema, and the output language of the listing text and tasks. Put all user-facing strings and the two field schemas in a small **per-market config** (e.g. `markets/it.py` + `markets/sk.py`, or a dict) so adding a third market later is config, not code.

---

## MODELS (all configurable, never hardcoded inline)

Put every model identifier in **config / env vars**, with these defaults, so they can be swapped without code changes:

| Purpose | Env var (suggested) | Default target |
|---|---|---|
| Live streaming transcription | `REALTIME_TRANSCRIBE_MODEL` | `gpt-realtime-whisper` |
| Transcript → structured extraction (reasoning) | `EXTRACTION_MODEL` | `GPT-5.6 Terra`-class reasoning model |
| Property photo editing/enhancement | `IMAGE_EDIT_MODEL` | `gpt-image-2`-class model |

**Verify the exact model strings before shipping.** `gpt-realtime-whisper` is the confirmed streaming-transcription model. The extraction and image model names above are targets the operator specified — **confirm the exact current API model ID in the OpenAI dashboard/docs and set it in the env var**; do not assume the literal string is correct. Fail loudly with a clear error if a configured model ID is rejected by the API, rather than silently falling back.

---

---

## WHAT THE FEATURE DOES (user story)

A real-estate agent (in **Italy or Slovakia**, depending on the customer's `market`) runs a listing-intake meeting with a property seller. All labels and generated text appear in that market's language — **Italian** (`it`) or **Slovak** (`sk`). Inside the dashboard, the agent:

1. Sees a **GDPR consent step** in the market language and gets the seller's spoken/checkbox consent to transcribe.
2. Taps **"Inizia" / "Spustiť"** — a **live transcription session** starts. As the seller and agent talk, the conversation appears on screen as **running unstructured text, in real time**, in the market language. The **screen stays awake**. The agent can **pause and resume** transcription at any time.
3. Taps **"Termina riunione" / "Ukončiť stretnutie"** — the accumulated transcript text is sent through a single AI pass that produces:
   - **structured listing fields** (using that market's field schema),
   - a **list of missing required fields** ("cosa manca" / "čo chýba"),
   - a **draft listing description** in the market language,
   - a **to-do list** of commitments made in the meeting.
4. Reviews an **editable confirmation screen** (nothing is saved silently), fixes any mis-heard values, and confirms.
5. Optionally **uploads property photos**, which are **AI-enhanced** (declutter / relight / straighten).
6. Ends with: confirmed listing data + listing text + task list + enhanced photos, all attached to a new property record for that customer.

**Note on audio:** the design is **live transcription, not record-then-transcribe**. Do **not** persist the raw audio by default — only the resulting transcript text is stored. (This is also cleaner for GDPR: you keep a text record, not a recording of the seller's voice.)

**Market labels:** Italy → "Acquisizione" / "Nuovo immobile"; Slovakia → "Nová nehnuteľnosť" / "Získanie". Pull these from the per-market config, don't hardcode inline.

---

## PHASE PLAN (implement in this order, each independently testable)

### Phase 1 — Live transcription session + wake lock (frontend + a thin backend token endpoint)
- New dashboard page/section (market-labelled).
- **Consent gate first.** Show GDPR text **in the customer's market language (it/sk)** explaining the meeting will be transcribed to prepare the listing. Both markets are under EU GDPR, same legal basis — only wording changes; keep both texts in the per-market config. Require an explicit action (checkbox + button, and/or a spoken consent captured at the very start). **Log the consent** (customer id, market, timestamp, method) to the backend before transcription can start.
- **Live transcription via OpenAI Realtime, WebRTC from the browser**, using the streaming transcription model **`gpt-realtime-whisper`** (model name via config — see Models section). Architecture:
  - Open a **transcription-only** Realtime session: `session.type = "transcription"`, with `audio.input.transcription.model` = the configured realtime-whisper model and `audio.input.transcription.language` = the record's market (`"it"` or `"sk"`), never hardcoded. Optionally set the `delay` param (`minimal|low|medium|high|xhigh`) to trade latency vs. accuracy — default to `low`.
  - **Never expose the OpenAI API key to the browser.** Add a small backend endpoint (e.g. `POST /acquisizione/session-token`) that mints a short-lived **ephemeral client token** for the Realtime session and returns it to the frontend. The browser uses only the ephemeral token. Bind a privacy-preserving safety identifier (hashed customer id) on the server side when creating the token.
  - Capture the mic with `getUserMedia`, connect via WebRTC, and **append incremental transcript deltas** (`conversation.item.input_audio_transcription.delta`) into a growing text buffer rendered live on screen as plain, unstructured text. Auto-scroll; keep it readable.
  - **Pause/Resume:** on pause, mute/disable the local mic audio track (and stop appending) so nothing is transcribed; on resume, re-enable it. The transcript buffer persists across pauses. Make pause/resume instant and obvious in the UI.
  - **60-minute session cap:** a single Realtime session maxes out at 60 minutes. For long meetings, detect the approaching limit (or a dropped connection) and **transparently open a fresh session and keep appending to the same transcript buffer** — the agent should never lose text or notice a seam. Also auto-reconnect on transient network drops.
  - Persist the transcript buffer defensively (e.g. periodic autosave to the backend or local state) so a crash mid-meeting doesn't lose everything.
- **Screen Wake Lock**, acquired *inside the same click handler* that starts the session (user-gesture requirement):
  - Feature-detect `'wakeLock' in navigator`.
  - **Re-acquire on `visibilitychange`** when the document becomes visible again (auto-releases when the tab is hidden — the #1 gotcha).
  - Wrap in try/catch (OS may refuse on low battery / power-saver).
  - **Fallback** to a NoSleep.js-style invisible-video trick when Wake Lock is unavailable (iOS < 16.4, installed-PWA iOS < 18.4, legacy Android).
  - Release the lock when the meeting ends.

### Phase 2 — Finish meeting (hand transcript to extraction)
- "Termina riunione / Ukončiť stretnutie" closes the Realtime session, releases the wake lock, and sends the **accumulated transcript text** (not audio) to the extraction endpoint in Phase 3.
- The transcript is the source of truth stored on the record. Slovak realtime transcription is generally good but can trail Italian on accented speech/domain terms — the editable review step in Phase 4 is the safety net, so keep it.
- No audio file is uploaded or stored (per the design note above).

### Phase 3 — Single structured extraction (backend), per-market
One AI call over the **transcript text** (from the live session) that returns **strict JSON** (use structured/JSON output; validate server-side with a schema, e.g. Pydantic). Use the configured **reasoning/extraction model** (see Models section — default target is the `GPT-5.6 Terra`-class model). **The field set, the required-fields list, the extraction prompt, and the output language all come from the record's market config.** The two markets share most fields but differ on the legally-mandatory ones — most importantly the **energy certificate** (Italy: *classe energetica / APE / IPE*; Slovakia: *energetický certifikát / energetická trieda*). Use a **shared common schema** plus a **per-market extension + per-market `required` list**.

**Common fields (both markets):**
```json
{
  "tipologia": "string|null",          // appartamento / byt, villa / dom, ...
  "indirizzo_o_zona": "string|null",
  "superficie_mq": "number|null",       // IT: mq  ·  SK: m² (úžitková plocha)
  "locali": "number|null",              // IT: locali  ·  SK: počet izieb (e.g. 3-izbový)
  "camere": "number|null",
  "bagni": "number|null",
  "piano": "string|null",
  "piani_totali": "number|null",
  "ascensore": "boolean|null",          // IT: ascensore  ·  SK: výťah
  "riscaldamento": "string|null",       // IT: autonomo/centralizzato · SK: individuálne/centrálne/plyn
  "stato_immobile": "string|null",      // IT: nuovo/buono/da_ristrutturare · SK: novostavba/dobrý/pôvodný/na rekonštrukciu
  "anno_costruzione": "number|null",
  "esposizione": "string|null",
  "spazi_esterni": "string|null",       // balcone/terrazzo/giardino · balkón/terasa/lodžia/záhrada
  "posto_auto": "string|null",          // box/posto auto · garáž/parkovacie miesto
  "cantina": "boolean|null",            // cantina · pivnica
  "arredato": "string|null",            // arredato/parziale/vuoto · zariadený/čiastočne/nezariadený
  "prezzo_richiesto": "number|null",    // EUR in both markets
  "note_venditore": "string|null"
}
```

**Italy-only extension:** `classe_energetica` (A4–G), `ipe` (kWh/m²a), `spese_condominiali` (€/mese), `tipo_proprieta` (piena/nuda).
**Slovakia-only extension:** `energeticka_trieda` (A0–G) + `energeticky_certifikat_esiste` (bool), `mesacne_poplatky` (mesačné poplatky / fond opráv, €/mesiac), `druh_vlastnictva` (osobné/družstevné — personal vs. co-op ownership, a real SK distinction with no IT equivalent).

**Per-market `required` (drives "missing" + blocking):**
- **IT:** `superficie_mq`, `prezzo_richiesto`, `classe_energetica`, `indirizzo_o_zona`, `spese_condominiali`.
- **SK:** `superficie_mq`, `prezzo_richiesto`, `energeticka_trieda`, `indirizzo_o_zona`, `druh_vlastnictva`.

**Return envelope (same shape for both markets):**
```json
{
  "market": "it|sk",
  "listing_fields": { /* common + that market's extension */ },
  "missing_required": ["classe_energetica", "..."],
  "listing_text": "draft description in the market language",
  "tasks": [
    {
      "descrizione": "string (in market language)",
      "owner": "agente|venditore",   // agent | seller
      "scadenza": "ISO date or null",
      "blocca_pubblicazione": true,
      "citazione": "short verbatim snippet that justifies this task"
    }
  ]
}
```

Rules to bake into the prompt you send the model (localize per market):
- **Never invent** legally/financially load-bearing values (superficie, prezzo, energy class, ownership type, cadastral data). If not clearly stated, leave `null` and add to `missing_required`.
- **`missing_required`** = that market's required fields that were not stated. Drives the "cosa manca"/"čo chýba" UI and doubles as seller-owned tasks.
- **Tasks = explicit commitments only** — IT cues: "le mando", "controllo", "richiamo", "porto"; SK cues: "pošlem", "skontrolujem", "zavolám", "prinesiem". Not every topic discussed. Attribute `owner` correctly. Mark `blocca_pubblicazione: true` for anything that gates going live (e.g. missing energy class in either market).
- **Output entirely in the record's market language** (Italian for `it`, Slovak for `sk`) — field values, listing text, and task descriptions.

> Note for Phase 0: confirm these required-field lists with me — I may want to adjust which fields are hard blockers per market before you wire them in.

### Phase 4 — Review & confirm UI (frontend)
- All labels rendered from the **per-market string config** (it/sk) — the same screen must render fully in Italian or Slovak depending on the customer.
- Render `listing_fields` (common + that market's extension) as an **editable form**; visually flag `missing_required` fields (they need filling). Fields the model marked as heard should be clearly "please confirm" / "potvrďte prosím".
- Show the **draft listing text** in an editable textarea.
- Show **tasks** as an editable checklist grouped by owner (agent / seller), with due dates and a "blocca pubblicazione" badge. Let the agent edit/delete/add.
- **Nothing persists until the agent hits "Conferma."** Then save the confirmed record.

### Phase 5 — Photo upload + AI enhancement (backend + frontend)
- Upload multiple photos; call the configured **image-editing model** (see Models section — default target is the `gpt-image-2`-class model) for enhancement presets (declutter / relight / straighten). Keep this **optional and isolated** — a failure here must not affect the already-saved listing/tasks.
- Store originals + enhanced versions against the record. Show before/after.

---

## DATA MODEL (adapt to whatever the repo already uses)

A property-intake record belonging to a customer:
`id`, `customer_id`, `market ("it"|"sk")`, `created_at`, `consent {given_at, method}`, `transcript` (the live-captured text), `listing_fields (json)`, `missing_required (json)`, `listing_text`, `tasks (json)`, `photos [{original_ref, enhanced_ref}]`, `status (transcribing|processing|review|confirmed)`.

No `audio_ref` — raw audio is not persisted (live transcription only). Store `market` on the record (copied from the customer at creation) so the whole downstream flow and any later re-processing stay in the right language even if the customer's setting changes.

Persist using the **same storage the dashboard already uses**. If the app currently stores per-tenant data in GitHub CSVs, discuss with me whether this richer record belongs in a proper DB instead — propose, don't decide.

---

## DELIVERABLES
- The plan (Phase 0) for approval first.
- Then phased implementation, each phase runnable and testable on its own.
- A short `README` snippet: new env vars, how to enable the feature flag, how to run/test locally, and **how to switch a test customer between `it` and `sk`** to exercise both markets.
- **Verify both markets end-to-end**: the same flow must run fully in Italian for an IT customer and fully in Slovak for an SK customer, using each market's field schema and required-fields list.
- Inline comments only where non-obvious (wake-lock re-acquire, audio chunking, JSON validation).

## NON-GOALS (do not build now)
- Storing/exporting raw meeting audio (transcript text only).
- Realtime/live in-meeting AI *prompting* of the agent (e.g. whispering "ask about the energy class" mid-meeting) — the transcription is passive for now; this is a future v2.
- Publishing to Immobiliare.it or any portal.
- Buyer-facing tours / SMS follow-up (separate systems).
- Any change to existing auth, billing, or unrelated pages.

**Begin with Phase 0: explore the repo and give me the plan. Do not write feature code yet.**
