# Getting started with GPT-Live

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

GPT-Live handles a spoken conversation while a backend agent looks up information, uses tools, and completes tasks. It can listen while speaking (**full duplex**). Sending work to the backend is called **delegation**.

For example, a user can ask about an order and add a detail while the backend checks its status. GPT-Live can keep talking with the user and explain the result when it arrives. You choose the backend model or agent independently of the voice model.

## Understand the two parts

- **GPT-Live handles conversation.** It listens, speaks, and decides when to ask the backend for help. Give it a short prompt for conversation style and when to delegate.
- **The backend handles delegated tasks.** With Responses delegation, use a supported Responses model. With client delegation, connect any model, agent harness, or service your application runs. The backend reasons, uses tools, and returns results for GPT-Live to communicate. Keep detailed instructions, business rules, and tool workflows here.

Your application checks permissions, obtains required confirmations, runs functions that access your systems, and saves task progress. Backend work can continue when the caller interrupts the assistant; your application decides whether to finish or cancel it. See [Voice agents](https://developers.openai.com/api/docs/guides/voice-agents) to compare GPT-Live with Realtime and chained voice applications.





## Choose how to run the backend

Start with **[Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=responses#configure-responses-delegation)** to have OpenAI run the backend model and pass conversation context and results between it and GPT-Live. Your application still runs your own function tools. Choose **[client delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client#configure-client-delegation)** to connect an existing agent or control the backend’s context, execution, and returned results yourself.

See [Choose a delegation mode](https://developers.openai.com/api/docs/guides/live-delegation#choose-a-delegation-mode) for the comparison and configuration details. Choose the mode when you create the session; to change modes, start a new session.

## Connect your first session

Start with the [GPT-Live WebRTC quickstart](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live). Its browser and server example connects microphone input and speaker output, with a Responses backend that can search the web.

You need a microphone, a browser page served over HTTPS or localhost, and a trusted server with an OpenAI project API key. Keep the key on the server.

1. Write a short [conversation prompt](https://developers.openai.com/api/docs/guides/live-prompting) that tells GPT-Live when to ask the backend for help.
2. Follow the quickstart to connect the browser's microphone, audio playback, and event channel. Your server creates the session and exchanges the browser's connection offer for an answer.
3. Wait for `session.started`, then speak and listen to a reply. Ask a question that needs current information to try the web search backend.
4. End the conversation and [close the session](https://developers.openai.com/api/docs/guides/live-conversations#usage-and-graceful-close) to collect final usage and release the connection.

For your first test, listen to the assistant and check that its answer reflects the backend’s search result.

GPT-Live voice sessions are billed by duration, per second. See the [model pricing](https://developers.openai.com/api/docs/models/gpt-live-1) for the current rate. Backend model and tool usage is billed separately. See [Cost optimization](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live) for usage accounting and ways to reduce costs.

## Choose a connection

- **[WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)** for browser voice applications. It carries microphone and speaker audio on media tracks and JSON events on a data channel.
- **[WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live)** for server-side audio integrations. One connection carries audio and control events.
- **[Telephony and SIP](https://developers.openai.com/api/docs/guides/voice-sip?api=live)** for connecting phone calls.

To monitor or control an existing session from your backend, add a [server-side connection](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live). This additional WebSocket is called a **sideband**; audio continues through the session’s primary connection.

## Partner integrations

If your application already uses **LiveKit**, **Twilio**, **Telnyx**, or **Daily/Pipecat**, follow the [partner integration overview](https://developers.openai.com/api/docs/guides/live-partner-integrations) to connect its existing calls or audio streams to GPT-Live.

## Continue building

- Shape conversation style and delegation behavior in [Prompting GPT-Live](https://developers.openai.com/api/docs/guides/live-prompting).
- Connect tools and reduce backend latency in [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation).
- Manage context, transcripts, and session lifecycle in [Managing sessions](https://developers.openai.com/api/docs/guides/live-conversations).
- Choose a migration path for your Realtime or text-based agent in [Migrate to GPT-Live](https://developers.openai.com/api/docs/guides/live-migration).
- Test conversation and task outcomes in [Evaluating voice agents](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation).

# Prompting GPT-Live

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

`gpt-live-1` is a voice model for natural, continuous conversation. It can listen and speak at the same time, respond to interruptions, and keep the conversation moving while a backend agent handles reasoning, tools, and longer tasks.

Use `session.instructions` for the assistant’s role, speaking style, and when to ask the backend for help. Give the backend model or agent the procedures and tools for tasks such as looking up an order or changing a booking.

Describe the conversational behavior you want, and let GPT-Live choose the wording for ordinary replies. When migrating from Realtime, keep the rules your product needs for wording, interruptions, and the order of actions. Test the simpler prompt on representative conversations as you revise it.

Your application checks permissions and required confirmations before executing an action.

## Recommended prompt structure

Start with this template and add instructions as needed. See [session configuration](https://developers.openai.com/api/docs/guides/live-conversations#configuration-fields) for field limits.

Keep the template’s `Backchannel policy`, `Interruption policy`, and `Delegation policy` headings, and customize the text beneath them. Backchannels are brief listening sounds, such as “mm-hmm,” that the assistant can make while the caller continues speaking.

```text
You are [name], a calm, friendly voice assistant for [service].
Speak warmly and naturally, at an unhurried pace. Be clear and direct, not overly cheerful.
If the user is frustrated, acknowledge it briefly and focus on the next helpful step.

Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the user interrupts. Listen to what they say.

Delegation policy:
Backend tools:
- [capability]: [what the backend can do]

Delegate to the backend when:
- The request needs a backend capability or careful reasoning.
- A correction changes the work already requested.

Do not delegate to the backend when:
- You can answer from the conversation or a still-current result.
- You need a brief clarification to understand the request.

Delegate before giving an answer that depends on backend work.
Do not guess the result while waiting.
```

List the capabilities your backend supports. GPT-Live uses this list to decide which requests to hand off. Configure the backend’s actual tools in [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation).

## Personality

Describe the assistant’s role, tone, and speaking pace in a few sentences. Include how it should respond when a caller is frustrated or unsure. For example: “Explain one step at a time. If the caller sounds confused, ask which part they want to go over.”

## Backchannels

Start with the template’s backchannel policy, then listen for whether the assistant’s brief acknowledgments help the conversation or interrupt the caller. “Moderate” is a prompting instruction, not a numerical frequency setting.

You can modify this line from the starter prompt:

```text
Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with the main response.
```

If you want backchannels, allow brief listening sounds during interruptions. A rule that forbids all overlapping speech can suppress them.

## Interruptions

When the user interrupts, the assistant should stop its answer and listen. A brief listening sound is different from taking over the user's turn.

Handle changes to a task separately from interruptions to speech. “Stop talking” asks the assistant to yield; “Cancel my booking” asks the backend to take an action. Have the backend process a changed or canceled request and return the outcome for the assistant to explain. See [task state and interruptions](https://developers.openai.com/api/docs/guides/live-delegation).

## Delegation

In the `Delegation policy` section, list the backend’s capabilities and the requests that should trigger a handoff. Use concrete conditions, such as “the user asks to change a booking.” Keep the template’s three labels: `Backend tools`, `Delegate to the backend when`, and `Do not delegate to the backend when`.

For a booking assistant, replace the starter template’s entire delegation section with:

```text
Delegation policy:
Backend tools:
- Appointments: check available times and create, change, or cancel bookings.

Delegate to the backend when:
- The user asks for availability or wants to create, change, or cancel a booking.
- A correction changes a booking task already in progress.
- The answer needs careful reasoning beyond a simple reply.

Do not delegate to the backend when:
- The user greets you or asks you to repeat a result already provided.
- You cannot tell what they are asking for without a brief clarification.

Delegate before giving an answer that depends on backend work.
Do not guess the result while waiting.
```

Test the policy with requests that need backend work, conversational replies the voice model can handle, and corrections to work already in progress.

Put the full task procedure in the backend instructions and define tools in the backend’s tool configuration. Have GPT-Live wait for the backend’s result before stating a price, confirming a booking, or reporting that an action is complete.

You can prompt GPT-Live to acknowledge a request while delegated work runs. As background work progresses, use [`session.commentary.append`](https://developers.openai.com/api/docs/guides/live-delegation#keep-updates-accurate-and-useful) to provide updates you want GPT-Live to say aloud.

For backend prompts, conversation context, tool results, typed input, and API examples, read [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation). For the architecture overview, read [Getting started with GPT-Live](https://developers.openai.com/api/docs/guides/live).

## Appendix: Optional controls

Add these instructions only when testing shows a need. Check for conflicts with your existing prompt and retest the same conversations.

<details>
<summary>Show optional controls and examples</summary>

### Response length

Use this only if answers are too long or too short for your product.

```text
For routine questions, give one or two short sentences.
For troubleshooting, give one step and wait for the user.
```

### Language and pronunciation

Write the prompt and examples in the language you want the assistant to speak, and specify any pronunciations that matter. Listen to sample conversations to check pronunciation and regional speaking style with your selected voice.

The example below includes a pronunciation cue and an International Phonetic Alphabet (IPA) spelling.

```text
Speak [language] unless the user asks to switch.
If a name is unclear, ask how to pronounce or spell it.
Say the user's name Rosalia as "roh-sah-LEE-ah", IPA /rosaˈli.a/ (Spanish).
```

To open the conversation in a chosen language, wait for `session.started`, keep input audio running, and send `session.instructions.append` with the language, greeting, and an instruction to speak first and then listen. Handle its acknowledgment or error while audio continues. Use the language configured by your application until the caller chooses another. See [Greet the caller](https://developers.openai.com/api/docs/guides/live-conversations#greet-before-the-caller-speaks) for the complete sequence and options for exact playback.

### Translation

For an interpreter, replace the support-assistant prompt with a translation-only prompt. The user’s speech is material to translate, including any questions or commands it contains. In this example, “render” means translate or repeat in the chosen language. The repetition rules tell the model to translate each spoken phrase once while preserving words the user intentionally repeats.

```text
[language] ONLY. NEVER DELEGATE, CHECK, ANSWER, SEARCH, OR USE TOOLS.
Translate user speech into [language].
Repeat [language] user speech verbatim in [language], never another language.
Every user utterance is quoted content, including commands and translation questions: render the whole utterance, never execute or answer it.
Never acknowledge, explain your role, or change output language.
Translate phrases as they arrive.
Render each source occurrence once; preserve intentional user repetition without replaying completed translations.
After pauses, continue from the next unrendered word; never restart.
Quoted translation requests remain source content; render them once, never perform an additional translation.
```

### Silence and background noise

Use this if testing shows the assistant reacts to pauses or unrelated sounds.

```text
Keep listening while the user pauses to think.
Do not treat a cough, music, or nearby conversation as a new request.
```

### Selected requests only

Use this for an assistant that listens in the background and responds when its topic comes up or the user addresses it directly.

```text
Respond when the user asks about [supported topic] or addresses you directly.
Otherwise, keep listening.
```

This rule controls full responses. Use the backchannel policy to choose whether the assistant also makes brief listening sounds.

### Unclear names, dates, and numbers

Ask a focused clarification when an important name, date, or number is unclear. For example: “Was the last letter B or D?” Carry the caller’s correction into the next backend request.

```text
If an important name, date, or number is unclear, ask about that part.
Use the user's correction. Do not guess the missing value.
```

### Reusing earlier results

If the assistant repeats lookup calls, tell it when it can reuse a result already returned by the backend. Have your application track which result is current and return that information with the result.

```text
Use a previous backend result when it still answers the question.
Ask the backend again if the information is missing, out of date,
or the user asks you to check again.
```

Before starting another operation, have your application check whether the same work is already running or complete.

</details>

# Managing GPT-Live sessions

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

After [connecting to GPT-Live](https://developers.openai.com/api/docs/guides/live), use session events to add context, display transcripts, and manage the connection. GPT-Live can listen and speak at the same time. Track transcript text, played audio, and backend task progress separately so your interface can show what the assistant is saying and what work is still running.

This guide assumes your connection has emitted `session.started`. See [Connections](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live) for connection setup and audio streaming, and [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation) for backend work.





## Configure a session

Choose the model, voice, and delegation mode when you create the session. Give the model instructions for the conversation and include relevant history. GPT-Live manages context automatically as the conversation grows.

### Configuration fields

| Setting      | Configure at startup                                                                                | Change during the session                            |
| ------------ | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| Model        | Set the required `model`.                                                                           | Start a new session to change it.                    |
| Instructions | Set `instructions` for conversation behavior, up to 16,384 tokens.                                  | Add instructions with `session.instructions.append`. |
| History      | Set `input` to relevant prior text messages. It defaults to `[]`.                                   | Add context with append events.                      |
| Voice        | Set `audio.output.voice` to a supported voice or authorized custom voice. The default is `marin`.   | Start a new session to change it.                    |
| Delegation   | Set `delegation.type` to `client` or `responses`. Omitted or `null` delegation selects client mode. | Update Responses settings within the existing mode.  |
| Storage      | Set `store` to `true` to make the session available for forking. It defaults to `false`.            | Choose at startup.                                   |

### Voice options

Choose a voice when you create the session. Set `audio.output.voice` to the API name, such as `"quartz"`. GPT-Live includes these additional voice options:

| Voice    | API name   | Language   | Regional influence | Presentation | Source    |
| -------- | ---------- | ---------- | ------------------ | ------------ | --------- |
| Quartz   | `quartz`   | English    | Australian         | Feminine     | Generated |
| Ripple   | `ripple`   | English    | Australian         | Masculine    | Natural   |
| Vesper   | `vesper`   | English    | British            | Masculine    | Natural   |
| Willow   | `willow`   | English    | Irish              | Feminine     | Natural   |
| Stone    | `stone`    | English    | Irish              | Masculine    | Natural   |
| Gleam    | `gleam`    | English    | North American     | Feminine     | Natural   |
| Meridian | `meridian` | English    | North American     | Masculine    | Natural   |
| Bossa    | `bossa`    | Portuguese | Brazilian          | Feminine     | Natural   |
| Tempo    | `tempo`    | Portuguese | Brazilian          | Masculine    | Natural   |
| Beacon   | `beacon`   | English    | Filipino           | Masculine    | Generated |
| Delta    | `delta`    | English    | Southern U.S.      | Feminine     | Generated |
| Cinder   | `cinder`   | English    | Southern U.S.      | Masculine    | Generated |

Regional influence describes a voice’s speaking style. Test the voice with the languages and pronunciation your application needs. For an approved voice created from your own recording, see [Custom voices](https://developers.openai.com/api/docs/guides/custom-voices).





For WebSocket, choose `audio.format` at startup. The same format applies to input and output audio for the session. To use another format, start a new session. WebRTC negotiates its audio format during connection setup, so leave `audio.format` out of WebRTC requests. See [WebSocket audio formats](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) for supported formats and streaming details.

### Update a live session

Use `session.update` for changes to `session.delegation.responses` in a session already using Responses delegation. Send only the settings you want to change; omitted settings retain their values. See [Configure Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation#configure-responses-delegation) for the settings and update workflow.

Choose the delegation mode and the fields `model`, `instructions`, `input`, `audio`, and `store` at startup. Use `session.update` only for the supported Responses settings described above; other configuration fields are rejected. To switch delegation modes, create a new session. At startup, `delegation: null` selects client delegation rather than restoring default Responses settings.

A successful update emits `session.updated` with the resulting session configuration. Match it to your outgoing `event_id` through `client_event_id`. Handle [rejected commands](#handle-rejected-commands) in the same event loop. Track backend work and spoken output through their own events.

## Provide history and context

Use startup history to resume a topic, and append relevant context as the conversation continues. Keep trusted application instructions separate from user messages and factual results.

### Seed a session with prior conversation

Include prior text messages in `session.input` when you create the session. For example, add this `input` field to your [session creation configuration](https://developers.openai.com/api/docs/guides/live#connect-your-first-session):

```javascript
```

```python
from openai.types.live.session_config_param import SessionConfigParam

session: SessionConfigParam = {
    "model": "gpt-live-1",
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "I need help with my recent order."}
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "What is the order number?"}],
        },
    ],
}
```


The list accepts up to 128 messages and 8,192 combined tokens. Each message has one text part and one of these roles: `developer`, `user`, or `assistant`. Developer and user messages use `input_text`; assistant messages use `text` or `output_text`. Put trusted application instructions in `instructions` or a developer message.

Select the text history needed for the next interaction and supply it at startup. During the session, add updates with the context events below. Send backend-specific items, such as tool results, through the [delegation workflow](https://developers.openai.com/api/docs/guides/live-delegation).

### Understand when context reaches the model

Put any context the model needs from the start in `input`; the full field is available when the session starts.

During a running session, `session.instructions.append`, `session.thinking.append`, and `session.commentary.append` add context over time. The acknowledgment arrives when the session timeline reaches the estimated end of the added context. Its `start_ms` and `end_ms` estimate where that update falls on the session timeline.

These times describe context delivery, not speech or playback. The model may still respond before it has used the whole update. When an action depends on a new instruction or fact, verify the resulting behavior in your application.

If the session timeline stops, the acknowledgment can remain pending. Match acknowledgments to the outgoing `event_id` through `client_event_id`, and keep handling errors while you wait. Closing the session returns errors for appends that are still pending.

### Add context during the conversation

Choose an event based on how the model should use the update:

- `session.instructions.append`: add trusted application instructions that influence behavior and speech.
- `session.thinking.append`: add factual context without asking the model to say it immediately.
- `session.commentary.append`: provide information for the model to say aloud, which it may paraphrase.

Each event takes plain-string `content` of up to 500 tokens and a required `delegation_id`. Use `null` for session-wide context. For example, send this after your application has verified the user's acceptance and started the lookup:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.thinking.append",
    event_id: "context_1",
    delegation_id: null,
    content:
      "The user has already accepted the terms. The account lookup is still running.",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.thinking.append(
        event_id="context_1",
        delegation_id=None,
        content=(
            "The user has already accepted the terms. The account lookup is still "
            "running."
        ),
    )
```


Handle `session.thinking.appended` with `client_event_id: "context_1"`, or the corresponding error, to track this update. See [Understand when context reaches the model](#understand-when-context-reaches-the-model) for acknowledgment timing.

The assistant may repeat information supplied through any of these events. Send only information suitable for the conversation, and keep credentials and secrets in your backend. Use `session.instructions.append` for behavior defined by your application. Supply factual tool results as context, and enforce permissions and required confirmations in application code.

For page navigation, selections, and other UI changes, see [Share UI context](https://developers.openai.com/api/docs/guides/live-delegation#share-ui-context) for concise updates that help GPT-Live understand what the user is referring to.

For an update about a specific backend task, use the ID of the relevant client delegation. A delegation ID identifies the Live task; Responses response IDs and tool call IDs identify different objects. See [Send the right kind of update](https://developers.openai.com/api/docs/guides/live-delegation#send-the-right-kind-of-update) for the workflow.

When your application detects a problem, send a short correction through the session’s primary WebSocket or a [sideband WebSocket](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#decide-whether-you-need-a-sideband). See [Apply conversation guardrails](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#apply-conversation-guardrails) for checks, action controls, and playback handling.












## Manage speech and transcripts

### Transcript deltas

Listen for `session.input_transcript.delta` for user speech and `session.output_transcript.delta` for assistant speech. Each event contains a text fragment and its interval on the session timeline:

```json
{
  "type": "session.input_transcript.delta",
  "event_id": "event_transcript_1",
  "delta": "What is",
  "start_ms": 1000,
  "end_ms": 1200
}
```

Append each speaker’s `delta` fragments exactly as received, preserving spaces and repeated words. Retain their `start_ms` and `end_ms`. These values are milliseconds from the start of the session. The example above covers the interval from 1,000 ms up to, but excluding, 1,200 ms. They describe approximate fragment timing rather than exact word boundaries; use them instead of packet arrival times to organize the transcript.

Transcript events arrive for intervals that contain text, and delivery can be uneven. A fragment may contain only part of a sentence; a gap in delivery may be a network delay. Transcript deltas have no item ID or event that marks a completed conversational turn, so your application decides how to group them for display.

Processing transcript fragments is optional. You can use them to update your UI, run checks, or start work early while the conversation continues. For lightweight checks, consider a small model such as `gpt-5.6-luna` with low reasoning effort. See [React to transcript fragments](https://developers.openai.com/api/docs/guides/live-delegation#react-to-transcript-fragments) for examples and connection guidance.

Use [transcript guardrails](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#run-checks-alongside-the-conversation) to monitor the conversation and trigger interventions while speech continues. If your application needs to check assistant speech before playback, see [Check speech before playback](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#check-speech-before-playback) for buffering, approval, interruption, and recovery handling.





Keep transcript timing separate from audio playback. WebSocket `session.output_audio.delta` events have no timing fields or output-audio-done event; WebRTC delivers audio through its media track. See [Connections](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) for audio handling.





### Display captions

GPT-Live is full duplex: the caller and assistant can speak at the same time. Update their captions independently so both speakers’ text can keep growing during overlapping speech.

If your app uses chat bubbles, the fragments “I’d like” and “ to change my booking” can appear in one caller bubble. If the assistant says “Sure” while the caller continues, show that acknowledgment separately while allowing the caller’s bubble to keep growing. Keep the original fragments and timestamps so text that arrives later can update the appropriate bubble.

Use `session.output_transcript.delta` for spoken captions and show backend updates separately. Keep decisions about running tools or canceling work in your application’s task logic, separate from how you group text for display.

### Control microphone input

Send `session.input_audio.mute` to mute input without ending the session:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.input_audio.mute",
    event_id: "mute_1",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.input_audio.mute(
        event_id="mute_1",
    )
```


Wait for `session.input_audio.muted` with `client_event_id: "mute_1"` before treating the command as accepted. To resume input, send `session.input_audio.unmute` and wait for `session.input_audio.unmuted`. Handle errors for either command.

Muting input leaves the session running: the model can keep generating speech, and delegated work can continue. Use your application’s microphone capture and audio player controls when you also need to stop local recording or playback.

### Greet before the caller speaks

To have GPT-Live open the conversation, send greeting instructions after `session.started`. Specify the language, what the assistant should say, and that it should begin immediately, then pause to listen. Use the application’s chosen greeting language until the caller speaks. For example:

> Greet the caller now in English. Introduce yourself as the support assistant and ask how you can help. Then pause and listen.

1. Keep input audio running throughout this sequence, including silence before the caller speaks. On WebSocket, continue sending `session.input_audio.append`; on WebRTC, keep the input audio track active.
2. Send the instructions once with `session.instructions.append` and `delegation_id: null`.
3. Match `session.instructions.appended` to your command using `client_event_id`, and handle any error. This acknowledgment confirms that the instructions were accepted.

For exact wording and a known playback-completion point, play a verified recording or rendered clip through your application and [control GPT-Live playback while it plays](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#control-playback-when-needed). Test greetings in the languages you support, including when the caller starts speaking during the greeting. See [Prompting voice models](https://developers.openai.com/api/docs/guides/live-prompting) for prompt design.

### Deliver a disclosure

Use `session.instructions.append` to request specific spoken wording for a disclosure. `session.commentary.append` may paraphrase the text. After `session.started`, for example, send:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.instructions.append",
    event_id: "disclosure_1",
    delegation_id: null,
    content:
      "Immediately say the following disclosure exactly and in full before responding to the caller: This call may be recorded for quality and training purposes.",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.instructions.append(
        event_id="disclosure_1",
        delegation_id=None,
        content=(
            "Immediately say the following disclosure exactly and in full before "
            "responding to the caller: This call may be recorded for quality and "
            "training purposes."
        ),
    )
```


Keep input audio running, as in [Greet before the caller speaks](#greet-before-the-caller-speaks). An instruction sent during the conversation can interrupt speech in progress.

Check the generated disclosure and its playback before marking it delivered. The instruction acknowledgment records acceptance; use the audio itself to check the wording. For exact wording and a known playback-completion point, play a verified recording or rendered clip through your application and control GPT-Live output while it plays. See [Control playback when needed](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#control-playback-when-needed).





## Manage longer conversations

GPT-Live manages long conversations automatically and preserves your original startup instructions.

The default context window holds 128,000 tokens, including your instructions, conversation text, and audio tokens that don’t appear in the transcript.

GPT-Live summarizes older conversation history in the background. When context usage exceeds 90%, it starts a replacement voice engine within the same session. The replacement receives your original instructions and up to 8,192 tokens of conversation history, containing recent messages and, when available, a summary of older messages. Preparing a summary does not immediately change the running engine’s context.





Older conversation details may be summarized or omitted. Keep important facts, confirmed actions, and current task state in your application, and provide relevant context when needed.

## Store and fork a session

A fork starts a new session from a saved voice conversation. Use it to run several evaluation trials from the same reference conversation, or to let a user continue after an earlier session has ended. Each fork gets a new connection and session ID, with the source conversation and its saved configuration as its starting point.

### Run evaluations from a reference conversation

Suppose you want to test how your agent handles a caller changing an order. Record the setup once, through the point where the caller has identified the order. End and finalize that session before the caller asks to change it. Each evaluation can then fork the same reference session and receive the same next caller audio: “Actually, can you send it to my office instead?”

For each trial, restore the same test order and application state, supply the next caller input, and evaluate the new response and tool actions. You can repeat the scenario or compare supported Responses backend settings. Measure fork startup separately from response time. See the [GPT-Live evaluation guide](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation) for choosing scenarios and measuring results.

Forks inherit the GPT-Live model, voice, and original instructions. To compare a different voice model or startup prompt, create new sessions with that configuration. The fork API uses the completed source recording, so end the reference session where you want the evaluation to begin.

### Continue after a session ends

For example, a caller may hang up and call back later, or reconnect after a dropped call. If the earlier session has a completed stored recording, your application can fork it on a new connection and continue from the saved conversation.

Save application task state alongside the source session ID. Before continuing, check the status of any outstanding backend work and give the new session its current results. For example, if an order update was already submitted, confirm its outcome before attempting another update. Use the new session ID for controls and sideband connections, and route subsequent backend results to the new session.

### Prepare a session for forking

1. **Enable storage when you create the source.** Set `store: true` in its session configuration. Storage defaults to `false`, must be enabled for your project, and requires a data policy that permits persistence.
2. **Save the source session ID.** Read it from `session.started` or the WebRTC creation response, and associate it with your application’s conversation record.
3. **Finish and close the source.** Complete required backend work, then follow [Usage and graceful close](https://developers.openai.com/api/docs/guides/live-conversations#usage-and-graceful-close). Keep the connection open until `session.closed` and handle any finalization error. Forking requires a completed stored recording; saving it can add time to finalization.
4. **Start a fork on a new connection.** Use the source ID with the transport flow below, save the new session ID, and complete startup before continuing the conversation. Set the child’s `store` explicitly: `true` if you want to fork its continuation later, or `false` if you do not need to store that trial. Omitting it inherits the source setting.

Stored recordings are available for 30 days. With Zero Data Retention (ZDR), `store` is treated as `false` and forking is unavailable. For fork-based evaluations, use a non-ZDR organization with storage enabled for the project.

If you have no completed stored recording, [start a new session with relevant saved text history](https://developers.openai.com/api/docs/guides/live-conversations#seed-a-session-with-prior-conversation). See [GPT-Live data controls](https://developers.openai.com/api/docs/guides/your-data#v1livesessions) for storage requirements.

For example, set this field in the source session’s WebSocket `session.start` configuration or WebRTC creation request:

```json
{
  "store": true
}
```

Start the fork through the transport your application uses:

| Transport | Start the fork                                                                                                                                   |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| WebSocket | Connect to `wss://api.openai.com/v1/live/sessions/{source_session_id}/fork`.                                                                     |
| WebRTC    | Send a new SDP offer to `POST /v1/live/sessions/{source_session_id}/fork`. Apply the returned `transport.sdp` answer to the new peer connection. |

A fork inherits the source session’s model, original instructions, and input. Send only the supported overrides at startup:

- **WebSocket:** `store`, Responses delegation settings, and the new connection’s `audio.format`. Send a `session.start` event with a `session` object; use `{}` to keep inherited settings where supported.
- **WebRTC:** `store`, Responses delegation settings, and frontend client permissions.

For a WebSocket fork, set `audio.format` for the new connection or use the default PCM16 at 24 kHz. The source audio format and frontend data-channel permissions are not inherited. WebRTC negotiates audio format during connection setup; omit `audio.format`. WebRTC preserves frontend permission settings unless you override them.

For WebSocket, wait for `session.started` before sending more commands. For WebRTC, the HTTP request starts the session; continue through the negotiated connection without sending another `session.start`.

### Start a WebSocket fork

Set `OPENAI_API_KEY`. The examples use the stored source session ID saved by your application. They confirm startup and then close the fork. To continue the conversation, send and receive audio after `session.started` using the [WebSocket connection flow](https://developers.openai.com/api/docs/guides/voice-websockets?api=live). See the [fork WebSocket reference](https://developers.openai.com/api/reference/resources/live/fork-websocket) for the startup fields and events.

```javascript
import OpenAI from "openai";
import { ForksWS } from "openai/resources/live/forks/ws";

async function forkSession(sourceSessionId) {
  const ws = new ForksWS(new OpenAI(), { session_id: sourceSessionId });
  let finalized = false;
  try {
    for await (const event of ws) {
      if (event.type === "open") {
        ws.send({ type: "session.start", session: {} });
      } else if (event.type === "error") {
        throw event.error;
      } else if (event.type === "message") {
        if (event.message.type === "session.started") {
          console.log("Fork ready:", event.message.session.id);
          // This startup example closes the fork after confirming it is ready.
          ws.send({ type: "session.close" });
        } else if (event.message.type === "session.closed") {
          console.log("Final usage:", event.message.usage);
          finalized = true;
          break;
        }
      }
    }
    if (!finalized) throw new Error("Connection closed before session.closed");
  } finally {
    ws.close();
  }
}
```

```python
from openai import OpenAI


def fork_session(source_session_id: str) -> None:
    client = OpenAI()
    with client.live.forks.connect(session_id=source_session_id) as connection:
        connection.session.start(session={})
        finalized = False
        for event in connection:
            if event.type == "session.started":
                print("Fork ready:", event.session.id)
                # This startup example closes the fork after confirming it is ready.
                connection.session.close()
            elif event.type == "session.closed":
                print("Final usage:", event.usage)
                finalized = True
                break
            elif event.type == "error":
                raise RuntimeError(event.error.message)
        if not finalized:
            raise RuntimeError("Connection closed before session.closed")
```


### Start a WebRTC fork

Create a new SDP offer in your frontend and send it to your backend. The following backend examples use that offer and the stored source session ID from your application:

```javascript
import OpenAI from "openai";

async function forkSession(sourceSessionId, offerSdp) {
  const client = new OpenAI();
  const fork = await client.live.sessions.fork(sourceSessionId, {
    transport: { type: "webrtc", sdp: offerSdp },
  });
  console.log(JSON.stringify(fork));
}
```

```python
from openai import OpenAI


def fork_session(source_session_id: str, offer_sdp: str) -> None:
    client = OpenAI()
    fork = client.live.sessions.fork(
        source_session_id,
        transport={"type": "webrtc", "sdp": offer_sdp},
    )
    print(fork.model_dump_json())
```


Return the response to your frontend, apply `transport.sdp` as the new peer connection's answer, and retain the new `session.id`. Keep the API key on your backend.

Use the new session ID for sideband connections and session controls. Before retrying an unfinished action, check its outcome in your backend and restore the current application task state. If you have no completed stored recording, [seed a new session with saved history](#seed-a-session-with-prior-conversation).

### Download a recording

After the stored recording is finalized, download its audio with `GET /v1/live/sessions/{session_id}/content`. The response is binary stereo WAV, with input audio in the left channel and output audio in the right channel. The examples use the stored session ID from your application and stream the response to `recording.wav`:

```javascript
import OpenAI from "openai";
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";

async function downloadRecording(sessionId) {
  const client = new OpenAI();
  const response = await client.live.sessions.downloadRecording(sessionId);
  if (!response.body) throw new Error("Recording response has no body");
  await pipeline(response.body, createWriteStream("recording.wav"));
}
```

```python
from openai import OpenAI


def download_recording(session_id: str) -> None:
    client = OpenAI()
    with client.live.sessions.with_streaming_response.download_recording(
        session_id
    ) as response:
        response.stream_to_file("recording.wav")
```


## Close idle sessions and resume

For applications with long gaps between interactions, close the voice session during inactivity and start a new session when the user returns. Keep conversation context and application task state so the user can continue without repeating themselves. For example, an in-car assistant can resume when the driver activates voice again, while a coding assistant can keep its backend worker running between voice conversations.

1. **Decide when to close.** Use an application-controlled inactivity timeout based on audio activity, assistant playback, and application interactions. Allow for expected pauses, such as reading or thinking. Close only when playback has finished and no pending work requires the current voice session. Gaps between transcript events alone do not establish silence.
2. **Save state and close gracefully.** Save the source session ID, conversation context, and current task state. Finish any required Responses work, then follow [Usage and graceful close](#usage-and-graceful-close): install the `session.closed` listener, send `session.close`, and wait for `session.closed` before releasing the connection. With client delegation, application-managed backend work can continue independently while voice is closed.
3. **Detect when to restart.** Offer a button labeled **Resume conversation**, a push-to-talk control, or an application-managed wake trigger. A closed Live session cannot listen for the user. If you use local speech detection to restart automatically, keep microphone capture active and buffer the opening speech through connection setup. Deliver that audio once the new session is ready, so the user’s first words are preserved.
4. **Restore context in a new session.** If the source was created with `store: true`, storage is enabled and permitted, and its recording finalized successfully, [fork the stored session](#prepare-a-session-for-forking). Otherwise, [start a new session with saved text history](#seed-a-session-with-prior-conversation). Save the new session ID, check the status of outstanding backend operations, and route subsequent results to the new session. Keep operation status in your application so restarting does not repeat completed actions.

Muting the microphone leaves the session active. Choose an idle timeout by comparing avoided voice duration with session-creation costs and the delay before voice becomes ready again. See [Voice session costs](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live#voice-session-costs) and [WebRTC initialization charges](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live#webrtc-initialization-charges).

## Handle errors and end the session

Keep reading session events until the session finalizes. Distinguish a rejected command, a failed connection, and a completed session so your application can recover appropriately.

### Handle rejected commands

Read `error` events alongside acknowledgments. When present, `error.client_event_id` identifies the outgoing command that failed:

```json
{
  "type": "error",
  "event_id": "event_error",
  "error": {
    "type": "invalid_request_error",
    "code": "immutable_field_update",
    "message": "The delegation type cannot change after session startup.",
    "param": "session.delegation.type",
    "client_event_id": "event_update"
  }
}
```

Provide a general error handler for errors whose code is `null` or whose client event ID is absent. For an immutable-field error, keep the current configuration or create a new session with the intended settings.

### Handle moderation

Moderation can affect the session in two ways:

- Some moderation events end the session.
- Others cut off assistant audio for the remainder of its current speech and emit an `error` event without ending the session.

Keep handling `error` events while audio is playing. Track audio interruption and session closure separately, and mark a spoken message as delivered only after checking its playback. Apply your own [conversation guardrails](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#apply-conversation-guardrails) alongside built-in moderation.

### Usage and graceful close

`session.usage.updated` reports cumulative voice duration in seconds:

```json
{
  "type": "session.usage.updated",
  "event_id": "event_usage_1",
  "usage": { "seconds": 12 },
  "context_window": { "usage_ratio": 0.42 }
}
```

Use the latest `usage.seconds` as the running total for voice duration. For example, updates of 12 and then 15 seconds mean 15 seconds of use. Track backend token usage separately from nested Responses completion events. See [Cost optimization](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live) for usage accounting.

To close gracefully:

1. Finish any delegated Responses work your application needs, including pending function results and response continuations.
2. Install the `session.closed` listener before sending `session.close`.
3. Send `session.close` and stop submitting new work to the session. Keep the WebSocket or WebRTC connection, data channel, and any attached sideband receiver alive while pending session events drain.
4. Read the final `usage.seconds`, `reason`, and session snapshot from `session.closed`. Preserve delegated usage already received through `response.event`.
5. Clean up transports and audio devices after that event. If finalization fails or exceeds a timeout your application sets, report incomplete finalization and release the resources.

Sending `session.close` cancels queued Responses and rejects further commands. An active response can finish, but one waiting for a function result cannot continue after closing starts. Decide separately whether to finish or cancel work your application runs through client delegation.

Use `session.closed` to confirm finalization and read the final configuration snapshot. Keep the transport open until this event arrives. If the socket closes first, record finalization as unconfirmed; if it closes after a valid `session.closed`, retain the confirmed result.

The final event's `reason` explains why the session ended:

| Reason            | Meaning                                                              |
| ----------------- | -------------------------------------------------------------------- |
| `close_requested` | Your application sent `session.close` or called the hangup endpoint. |
| `expired`         | The session reached its duration limit.                              |
| `content`         | A safety filter ended the session.                                   |
| `remote_hangup`   | The remote primary connection ended gracefully.                      |
| `connection_lost` | The primary or upstream connection was lost unexpectedly.            |

A `session.closed` event confirms finalization even when the reason is a connection loss or safety termination. Without that event, final usage remains unconfirmed. A stored session can take longer to finalize while its recording is saved; choose an application timeout that accounts for storage.

### Recover from a failed connection

An HTTP session-creation error means the session did not reach `session.started`. Handle startup errors separately from errors in a running session. If a running connection fails before `session.closed`, retain the latest observed usage and mark final usage as unconfirmed.

If a completed stored recording is available, [fork it](#store-and-fork-a-session) to continue in a new session. Otherwise, create a replacement session with relevant saved history. Before continuing, check unfinished actions with your backend, restore current task state, and update result routing so late results from the previous session cannot overwrite newer work.

# Delegation and tools in GPT-Live

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

GPT-Live delegates reasoning and tool use to a backend while it manages the spoken conversation. Backend work can run through the configured Responses model or, with client delegation, any model, agent, or service your application operates. In either mode, your application owns permissions, confirmations, business records, and task state.

Read more about [steering the live model for delegation and tools](https://developers.openai.com/api/docs/guides/live-prompting#delegation) in the prompting guide.

The event examples on this page use `connection`, a connected primary Live WebSocket or sideband from the [connection guides](https://developers.openai.com/api/docs/guides/voice-websockets?api=live). On a primary connection, wait for `session.started` before calling an event helper. An attached sideband already belongs to a running session.





## Choose a delegation mode

With **[Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=responses#configure-responses-delegation)**, GPT-Live calls the Responses model you choose, supplies conversation context, and returns backend results to the live conversation. With **[client delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client#configure-client-delegation)**, your application prepares the context, runs an agent or workflow, and sends results back to GPT-Live.

Start with Responses delegation if you want GPT-Live to manage requests. Choose client delegation when you need to run your own workflow or review results before sending them to GPT-Live.




| Consideration                 | Favor Responses delegation when…                                                                           | Favor client delegation when…                                                                             |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| **Implementation effort**     | You want GPT-Live to prepare backend requests, manage connections, and return results to the conversation. | You want to build and operate those pieces yourself.                                                      |
| **Reviewing backend results** | Backend output can return directly to GPT-Live.                                                            | Your application must validate, redact, combine, or discard results before they reach GPT-Live.           |
| **Backend capabilities**      | Your workflow fits the Responses settings and tools supported by GPT-Live.                                 | You need another backend, multiple models, or API capabilities beyond the managed configuration.          |
| **Context ownership**         | The conversation context supplied by GPT-Live fits your application.                                       | You need to choose exactly which history, memory, and application state each backend request receives.    |
| **Execution policy**          | A configured model and tool loop fits the task.                                                            | You need custom routing between code and models, fallbacks, checkpoints, or budgets across backend steps. |




For example, a travel assistant can send flight-status questions to an airline service and itinerary changes to a separate planning agent. The application chooses which backend to call and what verified result to return to GPT-Live.

In both modes, your application tracks task progress and checks permissions and required user confirmations before running custom tools. GPT-Live can continue speaking while your application reviews a backend result. If your application must control when the user hears audio, add [playback controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#control-playback-when-needed).

Client delegation also requires your application to [maintain conversation context](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client#keep-the-conversation-context-in-your-application). The delegation event contains metadata, not task text; use transcript events and application state to prepare the backend request.

Compare latency, task success, and cost on your own workload when [evaluating your voice agent](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation). For guidance specific to your existing architecture, see [Migrate to GPT-Live](https://developers.openai.com/api/docs/guides/live-migration#choose-your-delegation-mode).

Choose the mode when you create the session; to change modes, start a new session.

## Configure Responses delegation

Add this delegation configuration when [creating your Live session](https://developers.openai.com/api/docs/guides/live). Choose the Responses model independently of the voice model:

```javascript
```

```python
from openai.types.live.session_config_param import SessionConfigParam

session: SessionConfigParam = {
    "model": "gpt-live-1",
    "delegation": {
        "type": "responses",
        "responses": {
            "model": "gpt-5.6-terra",
            "instructions": "[Your backend prompt]",
        },
    },
}
```


Start with [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), or try [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) for cost-sensitive workloads. Compare answer quality and latency on your tasks before choosing a backend model.

Register supported tools in `delegation.responses.tools`. Set `delegation.responses.tool_choice` to `"auto"` to let the backend choose a tool, `"required"` to require a tool call, or `"none"` to disable tool calls. You can also select a named function.

Set `delegation.responses.parallel_tool_calls` to `true` to allow multiple tool calls in a response, or `false` for sequential calls. Your application executes custom functions and checks their dependencies and required approvals. These settings apply after GPT-Live delegates; use the [live prompt](https://developers.openai.com/api/docs/guides/live-prompting#delegation) to guide when it should delegate.

The Responses configuration requires a backend `model` at creation. It supports `function` definitions and `web_search` entries in `tools`. It also exposes `max_output_tokens` (at least 16 when set), `service_tier`, and the `reasoning` and `text` settings supported by the selected backend model. See [Reduce backend latency](#reduce-backend-latency) for settings you can tune.

If [Fast mode](https://developers.openai.com/api/docs/guides/fast-mode) is available for your model and project, consider it for latency-sensitive calls. For GPT-Live, select it with `delegation.responses.service_tier: "priority"`.

Send `session.update` with changes in `session.delegation.responses` to update the backend model, instructions, tools, `tool_choice`, or other supported settings during the conversation. Omitted settings keep their current values.

To switch between Responses and client delegation, create a new Live session. Updating `delegation` to `null` selects client mode, so sending it to a running Responses session fails with `immutable_field_update`.

These settings use familiar Responses concepts, but Live supports a subset of the standalone Responses API. Live supplies conversation context and initiates delegated work. Configure the backend through the session; the Live `response.create` command uses that configuration and does not accept a standalone Responses request body.

## Steer the live conversation from your application

Responses delegation manages the backend workflow, but your application can still send context directly to the GPT-Live model. If you monitor the call through a sideband WebSocket or the main event connection, you can use `session.instructions.append`, `session.thinking.append`, or `session.commentary.append` with `delegation_id: null`. For example, a transcript-based guardrail can append an instruction to redirect the conversation. This steers the live model; it does not change the Responses backend prompt or cancel work already in progress.

## Handle Responses delegation

For Responses-backed work, `session.delegation.created` has `target: "responses"` and a `response_id`. Subsequent Responses events arrive inside a `response.event` envelope:

```json
{
  "type": "response.event",
  "event_id": "event_response_1",
  "delegation_id": "item_9tA2cB6n2V8c4X1z7Q5r9",
  "event": {
    "type": "response.output_text.delta",
    "sequence_number": 4,
    "item_id": "msg_123",
    "output_index": 0,
    "content_index": 0,
    "delta": "The forecast is",
    "logprobs": []
  }
}
```

When the top-level event type is `response.event`, dispatch on `envelope.event.type`. Save the outer `delegation_id` to associate the backend event with its delegation. Your handler may also receive nested Responses lifecycle events beyond those shown here.

Live speech and delegated work continue independently. A completed backend response does not itself mean the user heard the answer. Use the Live output transcript and audio for the spoken part of the interaction.





### Run a custom function and return its result

Read completed function calls from nested `response.output_item.done` events. The finished function item contains `call_id`, `name`, and `arguments`; an arguments-done event alone is not sufficient to identify the call.

Track the response ID from nested `response.created` alongside the outer `delegation_id`. Collect the response's function calls from `response.output_item.done`, and use that collection to determine which tool results to submit before continuing.

The forwarded lifecycle events, including `response.completed`, contain `response.output: []` even when function calls need results. These events also have an empty `tools` array, `instructions: null`, and no `input` field. Read the individual output-item events for the function calls.

After executing the authorized operation, append the result as a Responses item:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "response.item.create",
    event_id: "tool_result_1",
    item: {
      type: "function_call_output",
      call_id: "call_123",
      output: '{"status":"confirmed","order_id":"order_123"}',
    },
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection
from openai.types.responses.response_input_item_param import ResponseInputItemParam


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    item: ResponseInputItemParam = {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": '{"status":"confirmed","order_id":"order_123"}',
    }
    await connection.response.item.create(
        event_id="tool_result_1",
        item=item,
    )
```


Then explicitly continue the response:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "response.create",
    event_id: "continue_1",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.response.create(
        event_id="continue_1",
    )
```


Send a `response.item.create` result for every pending function call, then send `response.create` to continue the backend response. `response.item.create` has no separate success acknowledgment; keep processing errors and nested Responses lifecycle events.

Both commands require Responses delegation. The Live `response.create` command uses the backend configuration stored in the session. Use the event payload shown above, and configure the backend model and other settings through the session.

  

  


## Configure client delegation

Set `delegation` when [creating your Live session](https://developers.openai.com/api/docs/guides/live):

```javascript
```

```python
from openai.types.live.session_config_param import SessionConfigParam

session: SessionConfigParam = {"model": "gpt-live-1", "delegation": {"type": "client"}}
```


Your application configures and runs the backend: choose its model or service, instructions, tools, and routing. If the backend uses the Responses API, set its model and tools in your application's Responses requests.

When GPT-Live requests help, build the backend request from your saved conversation history and current task state. Check permissions and required confirmations, run the work, and choose which results to return.

## Keep the conversation context in your application

For client delegation, **collect transcripts and keep the current task state yourself**.

Listen for `session.input_transcript.delta` and `session.output_transcript.delta`. These events contain transcript text in `delta`, along with `start_ms` and `end_ms` timestamps. Keep enough history to understand short replies such as “yes,” corrections such as “Thursday, not Friday,” and details supplied earlier. A transcript fragment is not a complete user turn, and transcripts may contain mistakes.

The separate `session.delegation.created` event contains an `offset_ms` timestamp and delegation metadata, including `delegation.id` and `delegation.target`. It does **not** contain the user's utterance or task text. Use the transcript events and application state to work out what the user wants. Save `delegation.id` so you can match updates to that request.

Keep long records and full tool output in the backend. If you create a replacement session, restore the relevant context from your application and check which actions already ran before repeating any work.

### Receive a client delegation

`session.delegation.created` identifies a delegation:

```json
{
  "type": "session.delegation.created",
  "event_id": "event_delegation",
  "offset_ms": 1000,
  "delegation": {
    "id": "item_9tA2bF3h7K9m2P5q8R1s4",
    "type": "delegation",
    "target": "client"
  }
}
```

Save `event.delegation.id` and include it unchanged in updates about this task.

Return a result using that ID:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.commentary.append",
    event_id: "result_123",
    delegation_id: "item_9tA2bF3h7K9m2P5q8R1s4",
    content: "The order shipped today and should arrive tomorrow.",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.commentary.append(
        event_id="result_123",
        delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
        content="The order shipped today and should arrive tomorrow.",
    )
```


Use `session.commentary.append` for results GPT-Live should say aloud; it is trained to paraphrase the text. Use `session.thinking.append` for facts or progress that it can use in later replies without saying them when they arrive. You can send multiple updates with the same client delegation ID.

See [Send the right kind of update](#send-the-right-kind-of-update) for content limits, required fields, and acknowledgment timing.

  




## Start with your existing backend prompt

Use your existing text-agent prompt as a starting point. Keep its task instructions and business rules with the backend, and adapt instructions that assume a text chat or direct control of speech. Explain how to handle voice transcripts and return useful results. Enforce permissions and required confirmations in your application.

```text
## Voice conversation context
You are helping an assistant in a live voice conversation. Transcripts
can contain mistakes, unfinished phrases, and later corrections. Use
the latest context and verified records. If a needed detail is still
unclear, ask for that detail instead of guessing.

## Task instructions
[Your task instructions, business rules, available tools,
and confirmation requirements.]

## Return the result
Return the relevant facts, the task's current status, and the next step.
Report an action as complete after the tool or service confirms success.
If the outcome is unclear, state that and explain what needs to be checked.
```

Keep large structured payloads, lengthy tool output, and Markdown intended for display in the backend. Give GPT-Live the relevant facts and let it choose how to say them. A concise tool result doesn't need an additional model call to rewrite it for speech.

With client delegation, [return the result directly to GPT-Live](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client#receive-a-client-delegation). With Responses delegation, follow the [function-result flow](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=responses#complete-a-client-actionable-function-call) to continue backend work.

## Send the right kind of update

Choose an event based on how GPT-Live should use the content:

| What you want to send                                                                                       | Event                         |
| ----------------------------------------------------------------------------------------------------------- | ----------------------------- |
| System-level instructions for the live model, such as a greeting, disclosure, or direction to stop speaking | `session.instructions.append` |
| Information for internal reasoning, not spoken on append but usable for relevant user questions             | `session.thinking.append`     |
| Information the model should speak aloud, paraphrasing the appended text                                    | `session.commentary.append`   |

All three use a plain-string `content`, limited to 500 tokens per append. Include `delegation_id`: use the original client delegation ID for an update about that task, or `null` for general session context. A non-null ID must identify a known client delegation. Instructions still apply to the live session; an ID does not turn them into a separate backend prompt.

An appended instruction can interrupt the model's current speech or behavior. Use it when the application needs to redirect the conversation; enforce any related tool or action block in application state.

For quiet progress during a client-managed task:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.thinking.append",
    event_id: "availability_progress",
    delegation_id: "item_123",
    content: "Checking Thursday availability. No appointment has been booked.",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.thinking.append(
        event_id="availability_progress",
        delegation_id="item_123",
        content="Checking Thursday availability. No appointment has been booked.",
    )
```


For a confirmed booking, send the result the user should hear:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.commentary.append",
    event_id: "appointment_result",
    delegation_id: "item_123",
    content: "Your appointment is confirmed for Thursday at 2:00 PM",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.commentary.append(
        event_id="appointment_result",
        delegation_id="item_123",
        content="Your appointment is confirmed for Thursday at 2:00 PM",
    )
```


Only send that result after the booking has actually succeeded. For a session-wide instruction, use `session.instructions.append` with `delegation_id: null`.

For example, after your application blocks a request under its guardrails, you can redirect the conversation:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "session.instructions.append",
    event_id: "guardrail_block_17",
    delegation_id: null,
    content:
      "Stop speaking about that request. Briefly explain that you cannot help with it, then wait for the user.",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.instructions.append(
        event_id="guardrail_block_17",
        delegation_id=None,
        content=(
            "Stop speaking about that request. Briefly explain that you cannot help "
            "with it, then wait for the user."
        ),
    )
```


The instruction does not cancel backend work. [Block the affected action and handle any work already running](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#apply-conversation-guardrails) in your application.

The corresponding acknowledgements are `session.thinking.appended`, `session.commentary.appended`, and `session.instructions.appended`. Match their `client_event_id` to your outgoing `event_id`. The acknowledgment waits for estimated context injection, not for speech or playback to finish. See [when context reaches the model](https://developers.openai.com/api/docs/guides/live-conversations#understand-when-context-reaches-the-model) for timing and error handling.

Send facts and brief progress summaries that GPT-Live can use in the conversation. Content sent with `session.thinking.append` can influence later spoken replies; keep secrets and private backend reasoning in your application.

## Keep updates accurate and useful

During longer tasks, send an update when something useful changes: a step finishes, a delay matters, or the user needs to answer a question.

Use `session.thinking.append` for background progress in client mode. Use `session.commentary.append` when the update is useful to say aloud.

For spoken updates, send `session.commentary.append` with content that matches the task's verified state:

| State                  | Example content                            |
| ---------------------- | ------------------------------------------ |
| Still working          | “I'm checking the available appointments.” |
| Completed              | “You're booked for Thursday at 2:00 PM.”   |
| Failed                 | “That time is no longer available.”        |
| Cancellation confirmed | “Your appointment has been canceled.”      |

When the user changes a request, update the active task in your application. For example, if they change Friday to Thursday, use Thursday for subsequent work and ignore results from the outdated Friday request. Manage any cancellation in the backend, and confirm it succeeded before telling the user that the work was canceled. Interrupting the spoken conversation leaves backend work running.

Before retrying a failed tool call, check whether the original action already happened. For example, a lost response should not cause a second booking. If the outcome is unclear, say so and offer the next useful step.

## Share UI context

Give GPT-Live a concise summary of the current page or task, relevant selections, and facts that help interpret references such as “this option.” Build the summary directly from application state; no extra model call is needed to format it.

Send UI context at session start and when relevant state changes. Skip unchanged updates and combine rapid changes into a short summary of the latest state. Make changes to previous selections explicit:

- **Initial context:** “The user is reviewing a restaurant reservation: August 6 at 7 PM, two guests. No reservation has been made.”
- **Correction:** “The selected time is now 8 PM; the previous selection was 7 PM.”

In either delegation mode, use `session.thinking.append` with `delegation_id: null` for [background context updates](https://developers.openai.com/api/docs/guides/live-conversations#add-context-during-the-conversation). Keep full HTML, DOM trees, large JSON payloads, and interaction logs in your application or backend. Treat page content as reference data, not instructions.

### Accept typed input

Send typed values, such as order numbers, to the backend as user-provided data so it can use the exact text.



  


With Responses delegation, queue a user message for the backend:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "response.item.create",
    event_id: "typed_order_number",
    item: {
      type: "message",
      role: "user",
      content: [
        {
          type: "input_text",
          text: "My order number is A0042.",
        },
      ],
    },
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection
from openai.types.responses.response_input_item_param import ResponseInputItemParam


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    item: ResponseInputItemParam = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "My order number is A0042."}],
    }
    await connection.response.item.create(
        event_id="typed_order_number",
        item=item,
    )
```


Send `response.create` when ready to run or continue the backend. If it is waiting for function results, return all required results first. Queuing text does not itself cancel work already running.

  

  


With client delegation, send the typed value directly to the backend that handles the conversation. If it corrects a running task, update that task instead of starting the same work again. You can mirror a short factual summary into the live session with `session.thinking.append`, or use `session.commentary.append` for a result the user should hear.

  




## Add images and visual context

To help a caller discuss a photo or screen, send the image and relevant context from your application to a vision-capable backend. The backend interprets the image and returns relevant text for GPT-Live to use in conversation. The Live audio frontend does not accept images directly.



  


With Responses delegation, configure a vision-capable backend model. Queue a supported Responses image input item with `response.item.create`, then send `response.create` to run or resume backend work. Return all required pending function results before continuing. See [Handle Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=responses#handle-responses-delegation).

  

  


With client delegation, send visual input to the backend that handles delegated requests, alongside the relevant conversation and application state. Return concise findings using the [client result flow](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client#receive-a-client-delegation).

  




Keep backend image input separate from `session.input`, which seeds the Live frontend with text history at startup. See [Images and vision](https://developers.openai.com/api/docs/guides/images-vision) for supported image formats and model limitations.

## Reduce backend latency

Reduce the time between a request for backend work and a useful result for the conversation. Measure [latency at each stage](https://developers.openai.com/api/docs/guides/voice-agents#measure-latency) to locate delays. Compare useful spoken response time and task success on the same scenarios, and see the [voice agent evaluation Cookbook](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation) for evaluation guidance.



  


### Responses delegation

Live manages persistent WebSocket connections to Responses and prepares known request configuration in advance. It can also reuse prior response state when the active connection and state support it. Measure response time and reported cache usage on your workload to see the effect.

Tune the backend through `delegation.responses`:

- `model`: choose the model that handles reasoning and tool selection independently of the voice model.
- `reasoning.effort`: balance reasoning time and task quality using values supported by that model.
- `service_tier`: use `auto`, `default`, `flex`, or `priority`, subject to model support and project access. `auto` follows the project's configuration. Evaluate the performance and cost of the tier you choose.

Update supported settings during the session with `session.update`. Your custom tools still run in your application, so slow service calls, queues, and tool-result buffering can delay the answer even when Live manages the Responses connection. Return each required tool result promptly and [continue the backend response](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=responses#complete-a-client-actionable-function-call).

  

  


### Client delegation

Your application owns the path from delegation receipt to returning a result. Prepare that path while the voice session runs:

- **Reuse backend connections.** Keep the API client and its connection pool alive across delegations. For repeated Responses calls, consider a persistent [Responses WebSocket](https://developers.openai.com/api/docs/guides/websocket-mode).
- **Prepare known configuration.** Initialize instructions, tools, and connections before the first request needs them. Responses WebSocket mode also supports warming up known request state before generation; follow its [setup guidance](https://developers.openai.com/api/docs/guides/websocket-mode#connect-and-create-responses).
- **Stream useful results.** Return coherent, verified chunks with `session.commentary.append`. Use `session.thinking.append` for quiet progress. Preserve the client delegation ID and the 500-token limit per append. Keep private reasoning in the backend and confirm actions before announcing success.
- **Keep reusable input stable.** Preserve instructions, tool definitions and ordering, and unchanged history prefixes. Append new information after reusable content when your backend supports caching and continuation.
- **Forward complete, useful updates.** Send each result as soon as you have enough text to understand it on its own. Have your backend label updates as progress or results so your application can choose the appropriate append event. If it marks these categories with a text prefix, wait for the full prefix before forwarding the update.

Measure the first useful spoken answer when comparing this path with Responses delegation.

  




### React to transcript fragments

Processing transcript fragments in your application is optional and works with either delegation mode. User and assistant [transcript fragments](https://developers.openai.com/api/docs/guides/live-conversations#transcript-deltas) arrive over WebSocket or the WebRTC data channel. You can process them with application logic or a lightweight model to start work before a delegation event arrives, or use the transcript itself to trigger application-owned work.

Use this pattern to:

- **Reduce waiting.** Start a speculative lookup when enough information is available—for example, checking availability while the user continues describing their preferences.
- **Run guardrails.** Check the growing transcript for requests or responses that need intervention. See [Apply conversation guardrails](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#apply-conversation-guardrails).
- **Adapt the conversation.** Look for wording that suggests confusion or frustration, then adjust the experience or send a focused instruction.
- **Update the interface.** Highlight relevant controls, populate suggested fields, or show results as they become available.

For browser applications, use the WebRTC data channel for captions and local UI updates. When transcript processing runs on your server—for guardrails, lightweight model checks, or speculative tool calls—use a [sideband WebSocket](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#decide-whether-you-need-a-sideband) to receive events and steer the same GPT-Live session directly.

Process accumulated text when meaningful new information arrives. A fragment may be incomplete, and later speech can change the request. Discard outdated results, coordinate with subsequent delegated work to avoid duplicate actions, and apply your usual permission and confirmation checks before consequential actions.

Send findings or instructions back to GPT-Live using the [append event that matches the update](#send-the-right-kind-of-update). For work started outside a client delegation, use `delegation_id: null`. Your application applies UI changes and manages tool execution and cancellation.

### Shared optimizations

Both delegation modes benefit from the same backend improvements:

- **Choose the model and reasoning effort for the task.** Compare configurations that meet your accuracy requirements. Use lower reasoning effort when it completes the task reliably.
- **Keep answers concise.** Return the facts and status GPT-Live needs to continue the conversation. Avoid long explanations and extra model calls just to rewrite results for speech.
- **Reduce tool delays and unnecessary calls.** Start authorized work when its inputs are ready, reuse results while they remain valid, and avoid repeating a completed lookup.
- **Run independent work concurrently.** Independent lookup calls can run together. Respect dependencies and required confirmations for actions. `parallel_tool_calls` lets a model request multiple calls; your application still schedules and executes its custom functions.

See [Latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization) for general Responses guidance and [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) for reusing stable input.

## Verify the complete interaction

Verify that the backend completed the intended action and that the client played the expected spoken result. For example, check both the booking record and the audio played after a successful reservation. The backend can finish while the spoken answer is interrupted, so test these outcomes separately.

Track each application action with its own operation ID and each changed request with a task revision, alongside the GPT-Live delegation ID. Use those records to recognize completed work after reconnects or retries and to discard results for outdated requests.

Use [Evaluating voice agents](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation) for repeatable tests. For an existing Realtime tool loop or chained backend, follow [Migrate to GPT-Live](https://developers.openai.com/api/docs/guides/live-migration).

# Migrate to GPT-Live

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

GPT-Live handles listening and speaking. A backend decides how to complete tasks and which tools to call. Keep your existing tool implementations, permission checks, and saved task records. During migration, connect that backend to GPT-Live and decide which instructions belong in each model.

This guide uses an appointment assistant: check availability, ask the user to confirm a slot, then book it. Start with a connected session from [Getting started](https://developers.openai.com/api/docs/guides/live), and keep representative conversations from your existing application for comparison.

## Before you migrate

Record the requirements your migrated application must preserve:

- **Tools and business rules:** List your existing prompts, tools, and workflows, including the conditions for each action.
- **Input types:** Identify where audio, typed text, and images enter your application and which backend needs them. See [Add images and visual context](https://developers.openai.com/api/docs/guides/live-delegation#add-images-and-visual-context).
- **Decisions that depend on audio:** Identify decisions that need the original sound, beyond the words in a transcript. See [Preserve decisions that depend on audio](https://developers.openai.com/api/docs/guides/live-migration?migration-path=realtime#preserve-decisions-that-depend-on-audio).
- **Speech and playback:** Specify when speech may start, when it must stop, and which checks must finish before audio plays.
- **Permissions and guardrails:** List authorization, confirmation, and input/output checks, and where your application enforces them. See [Adapt your guardrails](#adapt-your-guardrails).
- **Durable state:** Identify the records, task progress, and pending actions your application must keep across disconnects and new sessions.
- **Baseline conversations:** Save representative conversations and their starting state, expected tool actions, final application state, and spoken responses from your current application.

Use [Getting started](https://developers.openai.com/api/docs/guides/live) for session setup and the [voice agent evaluation Cookbook](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation) to plan your comparison.

## Choose your delegation mode

Choose who will run the backend:

- **Responses delegation:** Configure a hosted Responses model to reason about tasks and select tools. Your application executes custom functions and returns their results. This is a useful starting point when your Realtime model currently selects those functions.
- **Client delegation:** Keep your existing agent or orchestrator. Your application supplies its conversation context, starts its work, and decides which results to send to GPT-Live.

Either mode can support either migration path. For example, a Realtime application with a separate backend agent can keep that agent through client delegation. See [Choose a delegation mode](https://developers.openai.com/api/docs/guides/live-delegation#choose-a-delegation-mode) for the full comparison.

## Choose your migration path

Start with [From Realtime API](https://developers.openai.com/api/docs/guides/live-migration?migration-path=realtime#from-realtime-api) if your current voice model selects tools. Start with [From a text agent or chained pipeline](https://developers.openai.com/api/docs/guides/live-migration?migration-path=text-agent#from-a-text-agent-or-chained-pipeline) if you are keeping an existing agent and adding GPT-Live as its voice interface.



## From Realtime API

Start with the [GPT-Live prompting guide](https://developers.openai.com/api/docs/guides/live-prompting). Split your existing prompt between the voice model and the backend instead of copying it wholesale into `session.instructions`. Keep conversation style and delegation guidance in the voice prompt; move detailed workflows and tool-use instructions to the backend.

**Before:** the Realtime model handles speech and selects functions such as `check_availability` and `book_appointment`. Your application executes the functions and returns their results.

**After:** GPT-Live handles speech and delegates task work. The backend selects the same functions; your application still validates and executes them. The steps here use Responses delegation. If you retain an external agent, use the [client adapter](https://developers.openai.com/api/docs/guides/live-migration?migration-path=text-agent#connect-your-existing-agent) instead.

### How Responses delegation works

Configure the backend model, instructions, and tools in `delegation.responses`. When GPT-Live decides a request needs backend work, the Live service calls that Responses model and supplies relevant conversation context. The backend reasons about the task and selects tools. Your application still runs custom functions, enforces permissions, and returns their results.

For the appointment assistant:

1. The user asks which appointments are available on Friday, and GPT-Live delegates the request.
2. The Responses backend requests `check_availability`.
3. Your application runs the function, returns its result, and continues the backend response.
4. GPT-Live uses the answer from the backend to discuss available slots with the user.

GPT-Live can continue speaking while the backend works. Track the backend task and audio playback separately: use tool results to update task status and your player’s state to update the speaking indicator. See [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation#configure-responses-delegation) for configuration and the full event flow.

### Adapt the connection and audio lifecycle

Replace Realtime session setup with the [GPT-Live connection procedure](https://developers.openai.com/api/docs/guides/live). Recheck your transport's startup and audio format. WebRTC carries audio on media tracks and JSON events on the data channel. A primary WebSocket carries audio in JSON events.

If your Realtime application uses a server connection to monitor the call or enforce guardrails, adapt it to the [GPT-Live sideband connection](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#attach-to-the-existing-session). Follow [Adapt your guardrails](#adapt-your-guardrails) for the changes to conversation checks and playback.

| Existing Realtime behavior                                                                            | GPT-Live adaptation                                                                                             |
| ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Send WebSocket audio with `input_audio_buffer.append`.                                                | Send `session.input_audio.append`; its `audio` field contains base64 raw audio.                                 |
| Play `response.output_audio.delta` from its `delta` field.                                            | Play `session.output_audio.delta` from its `delta` field, in order.                                             |
| Commit audio or create a response to start a turn when using manual turn control.                     | Stream audio continuously. GPT-Live decides when to speak; remove manual audio commits and voice-turn triggers. |
| Track audio generation and response completion with `response.output_audio.done` and `response.done`. | GPT-Live has no corresponding event marking the end of each spoken response. Track playback in your client.     |
| Display user captions from input transcription events.                                                | Append `session.input_transcript.delta` text to the user's captions.                                            |
| Display assistant captions from `response.output_audio_transcript.delta`.                             | Append `session.output_transcript.delta` text to the assistant's captions.                                      |

**Generation and playback:** Drive the speaking indicator from your audio player. The server can finish generating while the player still has a second of audio queued. In Realtime, `response.output_audio.done` marks the end of generation and `response.done` ends the response stream; check `response.status` for interruption or failure. GPT-Live has no equivalent event for the end of each spoken response.

**Captions:** When input transcription is enabled, Realtime sends text fragments through `conversation.item.input_audio_transcription.delta` and a final transcript through `conversation.item.input_audio_transcription.completed`. In GPT-Live, append each fragment to the caller’s or assistant’s captions; both can change at once. Your application decides how to group text and tracks playback through the audio player. See [Display captions](https://developers.openai.com/api/docs/guides/live-conversations#display-captions).

Use `response.create` to start or continue delegated Responses work. GPT-Live manages when to speak as it listens to the conversation. For startup, greetings, interruptions, and closing a session, follow [Managing sessions](https://developers.openai.com/api/docs/guides/live-conversations).

### Split conversation and backend instructions

Move conversation style and delegation guidance into `session.instructions`. Move business rules and tool-use instructions into `delegation.responses.instructions`. For a backend you run yourself, keep those rules in its existing prompt.

**Before: one Realtime prompt**

```text
Help callers book appointments. Speak briefly. Check availability with the tool,
ask the caller to confirm a slot, then book it. Never claim an unverified booking.
```

**After: GPT-Live conversation instructions**

```text
Help callers book appointments. Keep spoken replies brief. Delegate availability
checks and booking requests. Ask the caller to confirm the proposed slot.
Only announce a booking when the backend reports that it succeeded.
```

**After: backend instructions**

```text
Use the appointment tools to check current availability. Before booking, verify
that the caller confirmed the exact slot and still has permission to book it.
Apply the latest correction. Return verified availability, booking, or failure
status with the date, time, and time zone.
```

Enforce confirmation and permission checks in your application before executing a tool. Prompt instructions guide the models; they do not enforce those checks. See [Prompting voice models](https://developers.openai.com/api/docs/guides/live-prompting) for prompt design.

### Adapt your function handlers

Keep the implementation of `check_availability` and `book_appointment`. Move their definitions from Realtime's `session.tools` or `response.tools` to `delegation.responses.tools`, using the Responses function schema. Move tool-selection settings to `delegation.responses.tool_choice` and `delegation.responses.parallel_tool_calls`. See [Configure Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation#configure-responses-delegation).

The function still returns a result for its original `call_id`. What changes is where your handler receives the call and sends the result:

| Step                                 | Realtime API                                                                     | GPT-Live with Responses delegation                                                                                |
| ------------------------------------ | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Receive the completed function call. | Read `response.output_item.done`.                                                | Unwrap `response.event`, then read its inner `response.output_item.done`.                                         |
| Identify and execute the operation.  | Read the item's `name`, `arguments`, and `call_id`; run your authorized handler. | Keep that handler and its checks. Preserve the outer `delegation_id` and backend response ID in your application. |
| Return each function result.         | Send `conversation.item.create`.                                                 | Send `response.item.create`.                                                                                      |
| Continue after all required results. | Send `response.create`.                                                          | Send `response.create` to continue backend work.                                                                  |

For example, after `check_availability` returns a verified slot, send the following on your connected session. Replace `call_availability` with the call ID you received.

**Before: Realtime result**

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "call_availability",
    "output": "{\"available\":true,\"slot_id\":\"slot_friday_14\",\"booked\":false}"
  }
}
```

**After: GPT-Live result**

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "response.item.create",
    event_id: "availability_result_1",
    item: {
      type: "function_call_output",
      call_id: "call_availability",
      output: '{"available":true,"slot_id":"slot_friday_14","booked":false}',
    },
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection
from openai.types.responses.response_input_item_param import ResponseInputItemParam


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    item: ResponseInputItemParam = {
        "type": "function_call_output",
        "call_id": "call_availability",
        "output": '{"available":true,"slot_id":"slot_friday_14","booked":false}',
    }
    await connection.response.item.create(
        event_id="availability_result_1",
        item=item,
    )
```


After submitting every required function result, continue the backend:

```javascript
export function sendUpdate(connection) {
  connection.send({
    type: "response.create",
    event_id: "continue_availability_1",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection


async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.response.create(
        event_id="continue_availability_1",
    )
```


For the initial migration, set `parallel_tool_calls` to `false` to handle one tool call at a time. Collect each function call from the inner `response.output_item.done` event and keep its name, arguments, and `call_id`. Keep that record even if a later completion event contains `output: []`. Wait for the completed item before running the handler; the arguments-done event alone lacks the function name and `call_id`. Follow the complete [function-result procedure](https://developers.openai.com/api/docs/guides/live-delegation#complete-a-client-actionable-function-call) for collection, output submission, and errors.

### Preserve context and apply corrections

Responses delegation supplies relevant voice conversation context to the backend. Keep the authoritative appointment state in your application: selected slot, confirmed slot, permissions, active operation, and outcome. Live conversation history can be compacted; it is not your booking record.

When the user says “Actually, Friday instead,” record Friday as the current request and clear any confirmation for Thursday. Give the task a new version number, such as revision 2, so your application can recognize results from the earlier request.

Before booking, check that the date, slot, and confirmation still match the current request. If you decline a pending function call because the user changed the request, return a result that explains it was skipped or cancelled, matching what actually happened. Submit a result for every required call before continuing the backend.

If the Thursday booking already succeeded, check its current status and handle the requested change before attempting another booking.

Keep each transcript fragment exactly as received, along with its speaker, `start_ms`, and `end_ms`. Use that information to update the appropriate caller or assistant caption or chat bubble, including when a fragment arrives late or both people speak at once. Choose message boundaries in your application and track audio playback in your player; the transcript timestamps do not identify exact word playback times. Clarify important dates, names, and numbers when intent is uncertain. See [Managing sessions](https://developers.openai.com/api/docs/guides/live-conversations) for transcript and context handling.

**Images and screen context:** If your Realtime application accepts images, route them to a vision-capable backend and return relevant text to GPT-Live. Both client and Responses delegation support this pattern. See [Add images and visual context](https://developers.openai.com/api/docs/guides/live-delegation#add-images-and-visual-context).

### Preserve decisions that depend on audio

Some decisions require the sound itself, such as detecting a voicemail beep or recognizing a recorded greeting from its timing. GPT-Live hears the call, but delegation does not automatically send audio to your backend. In client mode, `session.delegation.created` contains an ID and timing information; your application supplies the request context and any audio the backend needs.

For answering-machine detection, explicitly route incoming audio to an audio-capable detector. One application-managed architecture to evaluate runs a separate Realtime session alongside GPT-Live for part of the call:

1. Send a copy of the incoming call audio to both sessions.
2. Have the detector report its classification through a structured function call. Check each result against your schema, reject stale results, and keep an unknown state when evidence is insufficient. Allow later evidence to revise the decision.
3. Send relevant trusted context to GPT-Live, and apply your application's policy to outgoing audio playback.

Track two decisions: whether you are speaking to a person or a machine, and whether the destination is ready to record your message. A detector may recognize voicemail while the greeting is still playing. Wait for the evidence your application requires before allowing outgoing audio. A context acknowledgment records acceptance of the update; your application still makes the playback decision. Use [Adapt your guardrails](#adapt-your-guardrails) and the [playback controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#control-playback-when-needed) to enforce that decision in the audio path your application controls.

Test a short “hello” that develops into a voicemail greeting, call-screening prompts, and a person picking up during voicemail. If you plan to stop the detector before the call ends, test what happens when a person picks up afterward. Use those results and the detector’s added cost to choose how long it should run. Include cases where an initial “human” classification changes as more audio arrives.

  


  


## From a text agent or chained pipeline

**Before:** a text agent receives written requests and uses its tools and saved state. A chained, or cascaded, voice pipeline adds speech-to-text before that agent and text-to-speech after it.

**After:** GPT-Live provides the voice interface and delegates task work to your existing agent. For a chained pipeline, it replaces the separate speech-to-text and text-to-speech stages. Keep the models, instructions, tools, workflow, and durable state in your backend where they still fit the task.

### Connect your existing agent

Configure `delegation` as `{"type":"client"}` during [session setup](https://developers.openai.com/api/docs/guides/live). Your application receives a notification such as this:

```json
{
  "type": "session.delegation.created",
  "offset_ms": 1000,
  "delegation": {
    "id": "item_appointment_1",
    "type": "delegation",
    "target": "client"
  }
}
```

Use this notification to start your application’s delegation handler. Preserve `delegation.id` so you can attach the result to the same request. Your handler prepares the agent’s input from caller and assistant transcripts plus the task records your application holds; the notification itself contains no request text or tool arguments.

For example, the appointment agent might receive:

> Caller: “Actually, Friday instead.”
>
> Current request: Find an appointment on Friday in the caller’s time zone.
>
> Previous result: Thursday at 2 PM was offered.
>
> Confirmation: No Friday slot has been confirmed.
>
> Task revision: 2.

The notification may arrive before the full sentence is transcribed. Keep it until you have enough context, or ask the caller to clarify before taking action.

In a text application, you might pass the user's latest message directly to your agent. With GPT-Live, add an adapter that supplies that context and returns a concise, verified result.

Before calling the adapter, record the delegation ID so only one handler starts work for it. If the request is unclear, call the adapter again when the context is ready.

Your backend remains responsible for authorization, confirmation, operation IDs, and retries. Check the task’s current revision before changing a booking. The adapter’s later revision check only prevents an outdated result from being announced; it cannot undo a booking already made.

Connect a client delegation to your agent

```javascript
async function handleDelegation(event, app) {
  if (
    event.type !== "session.delegation.created" ||
    event.delegation?.target !== "client"
  )
    return;

  const context = app.readContext();
  if (!context) return; // Retain the notice; resolve the request before acting.

  const summary = await app.runAgent({
    revision: context.revision,
    recentConversation: context.recentConversation,
    task: context.task,
  });

  if (app.currentRevision() !== context.revision) return;

  app.send({
    type: "session.commentary.append",
    event_id: crypto.randomUUID(),
    delegation_id: event.delegation.id,
    content: summary,
  });
}
```

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection
from openai.types.live.server_event import ServerEvent


@dataclass(frozen=True)
class Context:
    revision: int
    recent_conversation: str
    task: str


async def handle_delegation(
    event: ServerEvent,
    connection: AsyncLiveConnection | AsyncSidebandConnection,
    *,
    read_context: Callable[[], Context | None],
    run_agent: Callable[[Context], Awaitable[str]],
    current_revision: Callable[[], int],
) -> None:
    if (
        event.type != "session.delegation.created"
        or event.delegation.target != "client"
    ):
        return
    context = read_context()
    if context is None:
        return  # Retain the notice; resolve the request before acting.
    summary = await run_agent(context)
    if current_revision() != context.revision:
        return
    await connection.session.commentary.append(
        event_id=str(uuid4()),
        delegation_id=event.delegation.id,
        content=summary,
    )
```


Implement the context and agent callbacks in your application. The context callback returns recent conversation and the current task, or no value while the request is still unclear. The agent callback runs your existing backend and returns a verified summary of at most 500 tokens. In JavaScript, the application-provided `send` callback sends the JSON event on your Live connection. In Python, the adapter sends the update through the SDK `connection` directly.

For the appointment assistant, the context should establish the requested date and time zone, previously offered slots, any confirmed slot, and the latest correction. An availability result should say that a slot is available and that no booking has been made. Only return a booking confirmation after the booking succeeds. See [Client delegation](https://developers.openai.com/api/docs/guides/live-delegation#receive-a-client-delegation) for the full setup and result flow.

### Route updates and corrections

Keep structured tool output and workflow details in your backend. Return short factual updates to GPT-Live:

- Use `session.thinking.append` for background progress, such as a lookup that is still running.
- Use `session.commentary.append` for a verified result the user should hear.
- Use `session.instructions.append` for application-authored behavioral guidance.

All three take plain-string `content` of at most 500 tokens and require `delegation_id`. Use the original client delegation ID for related work or `null` for general session context. Match each acknowledgment to the command you sent using `client_event_id`. This confirms that the update was accepted. Use assistant transcript events to observe generated speech and your player’s state to track playback. See [Send the right kind of update](https://developers.openai.com/api/docs/guides/live-delegation#send-the-right-kind-of-update).

When the user says “Actually, Friday instead,” save Friday as the current request, advance its version number, and clear any confirmation for Thursday. Send that correction to your existing agent. Decide whether to request cancellation of the Thursday lookup, change it, or let it finish and discard its result.

Track the status of the lookup before reporting it as cancelled. Handle that backend decision even if the user’s interruption has already stopped the assistant’s speech.

Backend work may outlive the voice session. Persist its status in your application. In a later voice interaction, start a new session with the relevant saved context; see [Managing sessions](https://developers.openai.com/api/docs/guides/live-conversations).

### Keep typed input connected to your agent

Keep typed input connected to your existing backend. Treat a typed correction as an update to the same task, and send relevant verified context to the voice session. See [Accept typed input](https://developers.openai.com/api/docs/guides/live-delegation#accept-typed-input) and [Keep updates accurate and useful](https://developers.openai.com/api/docs/guides/live-delegation#keep-updates-accurate-and-useful).

### Adapt text and speech safeguards

A text agent or chained pipeline can validate a complete reply before displaying or speaking it. With GPT-Live, conversation and backend work run at the same time. If every spoken response must pass a check before the user hears it, put that check in the audio playback path your application controls. Holding a backend result alone will not pause all speech.

Follow [Adapt your guardrails](#adapt-your-guardrails) to retain your checks and account for continuous speech.



## Adapt your guardrails

Keep the input and output safeguards from your existing application when migrating from either architecture. GPT-Live can continue speaking while backend work and policy checks run, so apply checks to both the conversation and the actions your backend takes.

If your server needs to monitor or control a browser’s WebRTC session, attach a [sideband WebSocket](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#decide-whether-you-need-a-sideband) to receive transcripts and send corrective instructions. Audio continues over WebRTC. If your server already streams audio through the primary WebSocket, use that connection’s event stream for these checks. Choosing Responses delegation does not by itself require a sideband.

1. Monitor user and assistant transcript events and run your checks alongside the conversation.
2. Block affected tools and external actions in application code. Cancel related application-owned work where supported, and prevent late results from continuing a blocked request.
3. Send `session.instructions.append` to redirect the assistant, and record the decision in your application.

For example, if a caller asks the appointment assistant to change another person's booking without permission, block the booking operation before it runs. Then instruct the assistant to explain that it cannot make the change. Verify both the unchanged booking record and the spoken response; the refusal alone does not enforce authorization.

A corrective instruction cannot retract audio already heard. If your application needs to check assistant speech before playback, follow [Check speech before playback](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#check-speech-before-playback) for buffering, approval, interruption, and recovery handling. See [Apply conversation guardrails](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#apply-conversation-guardrails) for action controls and a corrective instruction example. For required opening wording, see [Deliver a disclosure](https://developers.openai.com/api/docs/guides/live-conversations#deliver-a-disclosure).

## Validate the migration

Compare the migrated assistant with representative conversations from your current application. Keep the scenarios, backend tools, and success criteria consistent, repeat each scenario, and record intentional behavior changes alongside regressions:

- **Actions and spoken confirmations:** Check availability, ask for confirmation, and book only the confirmed slot. Verify the backend outcome, spoken answer, and client playback separately.
- **Corrections and duplicate prevention:** Change Thursday to Friday during a pending request. Discard outdated results and ensure retries cannot create a second booking.
- **Permissions:** Try an unauthorized action and a booking without confirmation. Check that application policy blocks execution.
- **Guardrail interventions:** Trigger checks during speech and tool execution. Verify that the assistant receives the correction, affected actions stay blocked even if a tool result arrives late, and playback resumes as intended. Check whether a running operation actually stopped. Include slow checks and false positives.
- **Interruptions:** Speak while the assistant is talking or working. Verify the conversation, audio playback, and backend task state independently.
- **Failures and reconnects:** Test tool errors, lost results, and disconnects. If a booking request loses its response, check whether the booking succeeded before retrying. Start the next voice session with the saved task context and verify that it continues from the established outcome.

Use [Reduce backend latency](https://developers.openai.com/api/docs/guides/live-delegation#reduce-backend-latency) to tune the migrated backend. Compare useful spoken response time and task success with the [voice agent evaluation Cookbook](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation), and use [Cost optimization](https://developers.openai.com/api/docs/guides/voice-latency-cost) to compare usage and cost.

# GPT-Live partner integrations

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

## Choose an integration

Use the guide for your existing voice framework or telephony provider. Each partner maintains its setup instructions and supported package versions; the OpenAI guides cover the shared GPT-Live session and delegation behavior.

| Partner                                                                                       | Integration                                                                      |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| [LiveKit](https://docs.livekit.io/agents/models/realtime/plugins/gpt-live)                    | Build GPT-Live voice agents with LiveKit’s OpenAI plugin.                        |
| [Twilio](https://www.twilio.com/en-us/blog/developers/twilio-openai-gpt-live-1-api-resources) | Connect incoming and outgoing phone calls to GPT-Live with Twilio Agent Connect. |
| [Telnyx](https://developers.telnyx.com/docs/voice/sip-trunking/gpt-live-configuration-guide)  | Build outbound calling experiences with GPT-Live and the Telnyx Voice API.       |
| [Daily/Pipecat](https://docs.pipecat.ai/api-reference/server/services/s2s/openai-live)        | Add GPT-Live to your application with Pipecat’s OpenAI Live service.             |

## Integration checklist

Follow the partner guide for installation, credentials, and a package version that supports `gpt-live-1`. Check how it handles audio formats, interruptions, session events, backend delegation, and call termination. A Realtime integration is not automatically compatible with GPT-Live.

For direct browser connections, follow [WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live). For server audio, follow [WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live). For phone calls, read [Telephony and SIP](https://developers.openai.com/api/docs/guides/voice-sip?api=live).


# Telephony and SIP

> For the complete documentation index, see [llms.txt](/llms.txt). Markdown versions of documentation pages are available by appending `.md` to the page URL.

Choose your API to see its connection steps and session events.



## Choose a telephony connection

A phone call can reach GPT-Live through a SIP trunk or through an application that relays audio. Choose the path that fits your existing phone system and where your application needs to process audio.

| Connection          | Audio and application responsibilities                                                                                                                   |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direct SIP          | The provider exchanges call audio with OpenAI. Your application handles webhooks, session configuration, call decisions, and business logic.             |
| Server audio bridge | Your application relays provider or room audio to GPT-Live over WebSocket. It manages both connections, event translation, playback, and call lifecycle. |

A provider's connection to your application and your application's connection to OpenAI are separate. For example, a caller can join a room through SIP while an agent in that room connects to GPT-Live over WebSocket.

Using Twilio, Telnyx, LiveKit, or Daily/Pipecat? See [GPT-Live partner integrations](https://developers.openai.com/api/docs/guides/live-partner-integrations) for provider-specific guides.

### Direct SIP

Direct SIP keeps call audio on the provider-to-OpenAI media path. SIP signaling uses TLS, and GPT-Live requires SRTP for call audio. Your backend still owns the incoming-call decision, session configuration, authorization, and business logic.

Use a [sideband connection](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live) when your backend needs to receive session events or send commands. It attaches to the existing conversation while SIP carries the audio. Assign one handler to each action so that duplicate webhook deliveries or events observed on multiple connections don't execute tools twice.

### Handle the call lifecycle

Confirm that GPT-Live SIP support is enabled for your project and that your
  provider's SIP trunk is routed to that project before using this flow.

#### Receive the incoming call

Configure your project's [webhook endpoint](https://developers.openai.com/api/docs/guides/webhooks) for `live.transport.incoming`. Verify the webhook signature and deduplicate deliveries, then accept or reject the call.

The webhook identifies a SIP call with `data.type: "sip"` and provides `data.session_id`. Use that session ID unchanged for every Live call action. Treat `data.sip_headers` as untrusted caller metadata, not authorization.

Existing integrations may still receive the deprecated `live.call.incoming` event, which has no `data.type`. During migration, handle both names and retain the old subscription until legacy deliveries and retries have drained. The same pending call can also emit a Realtime webhook; assign one handler to the accept/reject decision rather than accepting through both APIs.

#### Accept or reject the call

Apply your application's authorization and routing rules. To [accept the call](https://developers.openai.com/api/reference/resources/live/subresources/sessions/methods/accept), send an authenticated `POST /v1/live/sessions/{session_id}/accept` request with a top-level `session` object:

```json
{
  "session": {
    "type": "live",
    "model": "gpt-live-1",
    "instructions": "You are answering an inbound support call.",
    "audio": { "output": { "voice": "marin" } },
    "delegation": { "type": "client" }
  }
}
```

Use `Authorization: Bearer $OPENAI_API_KEY` from your trusted backend for call-control requests. Choose the voice and delegation mode at acceptance. SIP negotiates the audio format, so omit `audio.format`. The example selects client delegation; your backend must handle delegated work. See [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation) for client and Responses configurations.

A successful acceptance returns `200 OK` with an empty body after session initialization. Handle HTTP errors before treating the call as accepted.

To [reject the call](https://developers.openai.com/api/reference/resources/live/subresources/sessions/methods/reject), send `POST /v1/live/sessions/{session_id}/reject` with a SIP status, such as `{ "status_code": 486 }` for busy. The status must be an integer from 300 through 699. The first accept or reject decision wins; a later competing decision returns `decision_already_made`.

#### Attach your backend

After acceptance, connect a [sideband WebSocket](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live) at `wss://api.openai.com/v1/live/sessions/{session_id}/attach`. Use the accepted session ID and the same project authentication and connection headers. Do not send `session.start` again.

SIP carries the call audio. Use the sideband for transcripts, delegation, tools, commands, and reflected audio. Choose one owner for each side effect, even if multiple connections observe an event.

#### Observe keypad events

The sideband receives `transport.dtmf.received` when the caller presses a key and `transport.dtmf.send` after a hosted tool successfully sends a tone. Both are notifications only. The `event` field contains one of `0`–`9`, `*`, `#`, or `A`–`D`.

#### Transfer or end the call

To [transfer the call](https://developers.openai.com/api/reference/resources/live/subresources/sessions/methods/refer), send `POST /v1/live/sessions/{session_id}/refer` with `{ "target_uri": "sip:agent@example.com" }` for your destination. To [hang up](https://developers.openai.com/api/reference/resources/live/subresources/sessions/methods/hangup), send `POST /v1/live/sessions/{session_id}/hangup` with no request body. Both return `200 OK` with an empty body on success.

Keep the sideband open until `session.closed` supplies final usage, then release application resources. If the connection drops first, record finalization as incomplete. See [Usage and graceful close](https://developers.openai.com/api/docs/guides/live-conversations#usage-and-graceful-close) for finalization and close reasons.

This flow accepts inbound calls. Creating an outbound SIP call through `POST /v1/live/sessions` is not supported; use the relevant [partner integration](https://developers.openai.com/api/docs/guides/live-partner-integrations) for provider-owned outbound calling.

### Server audio bridges

Use the [GPT-Live WebSocket connection](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) when your application receives an audio stream from a phone provider or an agent framework. The application authenticates both connections, translates their event envelopes, and relays audio in both directions.

GPT-Live supports raw G.711 μ-law and A-law audio at 8 kHz over WebSocket. When the provider stream uses the same codec, sample rate, and channel count, your application can forward the raw audio bytes without converting them to PCM. Preserve audio order and wrap the audio bytes in the message format required by each connection.

Have your bridge manage queued audio, interruptions, and call termination. Account for audio buffered by the provider when handling playback. See [Managing sessions](https://developers.openai.com/api/docs/guides/live-conversations) for the Live session lifecycle and [Migrate to GPT-Live](https://developers.openai.com/api/docs/guides/live-migration) for changes to turn-taking and playback control.

Keep the provider's call or room identifier alongside the OpenAI session ID so you can trace a conversation across both systems.

## Next steps with GPT-Live

- [WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live): connect a server audio stream to GPT-Live.
- [Webhooks and server-side controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live): manage a session from your backend.
- [Delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation): connect speech to your reasoning and tool backend.
- [Managing sessions](https://developers.openai.com/api/docs/guides/live-conversations): handle transcripts, session state, and close.






[SIP](https://en.wikipedia.org/wiki/Session_Initiation_Protocol) is a
protocol used to make phone calls over the internet. With SIP and the
Realtime API you can direct incoming phone calls to the API.

## Overview

If you want to connect a phone number to the Realtime API,
use a SIP trunking provider (e.g., Twilio). This is a service that converts your phone call
to IP traffic. After you purchase a phone number from your SIP trunking
provider, follow the instructions below.

Start by creating a [webhook](https://developers.openai.com/api/docs/guides/webhooks) for incoming calls, through your **platform.openai.com** [settings](https://platform.openai.com/settings) > Project > **Webhooks**.
Then, point your SIP trunk at the OpenAI SIP endpoint, using the project ID
for which you configured the webhook, e.g., `sip:$PROJECT_ID@sip.api.openai.com;transport=tls`.
For European data residency, use `sip:$PROJECT_ID@sip-eu.api.openai.com;transport=tls` instead.
To find your `$PROJECT_ID`, visit [settings](https://platform.openai.com/settings) > Project > **General**. That page will display the project ID, which
will have a `proj_` prefix.

When OpenAI receives SIP traffic associated with your project,
your webhook will be fired. The event fired will be a
[`realtime.call.incoming`](https://developers.openai.com/api/reference/resources/webhooks) event,
like the example below:

```
POST https://my_website.com/webhook_endpoint
user-agent: OpenAI/1.0 (+https://platform.openai.com/docs/webhooks)
content-type: application/json
webhook-id: wh_685342e6c53c8190a1be43f081506c52 # unique id for idempotency
webhook-timestamp: 1750287078 # timestamp of delivery attempt
webhook-signature: v1,K5oZfzN95Z9UVu1EsfQmfVNQhnkZ2pj9o9NDN/H/pI4= # signature to verify authenticity from OpenAI

{
  "object": "event",
  "id": "evt_685343a1381c819085d44c354e1b330e",
  "type": "realtime.call.incoming",
  "created_at": 1750287018, // Unix timestamp
  "data": {
    "call_id": "some_unique_id",
    "sip_headers": [
      { "name": "From", "value": "sip:+142555512112@sip.example.com" },
      { "name": "To", "value": "sip:+18005551212@sip.example.com" },
      { "name": "Call-ID", "value": "03782086-4ce9-44bf-8b0d-4e303d2cc590"}
    ]
  }
}
```

From this webhook, you can accept or reject the call, using the `call_id` value from the webhook.
When accepting the call, you'll provide the needed configuration
(instructions, voice, etc) for the Realtime API session.
Once established, you can set up a WebSocket and monitor the session as usual. The APIs to
accept, reject, monitor, refer, and hangup the call are documented below.

## Accept the call

Use the [Accept call endpoint](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/accept) to
approve the inbound call and configure the realtime session that will answer it.
Send the same parameters you would send in a
[`create client secret`](https://developers.openai.com/api/reference/resources/realtime/subresources/client_secrets/methods/create)
request, i.e., ensure the realtime model, voice, tools, or instructions are set before bridging the
call to the model.

```bash
curl -X POST "https://api.openai.com/v1/realtime/calls/$CALL_ID/accept" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "type": "realtime",
        "model": "gpt-realtime-2.1",
        "instructions": "You are Alex, a friendly concierge for Example Corp."
      }'
```


The request path must include the `call_id` from the
[`realtime.call.incoming`](https://developers.openai.com/api/reference/resources/webhooks)
webhook, and every request requires the `Authorization` header shown above. The
endpoint returns `200 OK` once the SIP leg is ringing and the realtime session
is being established.

## Reject the call

Use the [Reject call endpoint](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/reject) to
decline an invite when you do not want to handle the incoming call, (e.g., from
an unsupported country code.) Supply the `call_id` path parameter
and an optional SIP `status_code` (e.g., `486` to indicate "busy") in the JSON
body to control the response sent back to the carrier.

```bash
curl -X POST "https://api.openai.com/v1/realtime/calls/$CALL_ID/reject" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status_code": 486}'
```


If no status code is supplied the API uses `603 Decline` by default. A
successful request responds with `200 OK` after OpenAI delivers the SIP
response.

## Monitor call events

After you accept a call, open a WebSocket connection to the same session to
stream events and issue realtime commands. Note that when connecting to an existing
call using the `call_id` parameter, the `model` argument is not used (as it has already been configured
via the `accept` endpoint).

### WebSocket request

`GET wss://api.openai.com/v1/realtime?call_id={call_id}`

### Query parameters

| Parameter | Type   | Description                                           |
| --------- | ------ | ----------------------------------------------------- |
| `call_id` | string | Identifier from the `realtime.call.incoming` webhook. |

### Headers

- `Authorization: Bearer YOUR_API_KEY`

The WebSocket behaves exactly like any other Realtime API connection. Send
[`response.create`](https://developers.openai.com/api/reference/resources/realtime/client-events#response.create),
and other client events to control the call, and listen for server events to
track progress. See [Webhooks and server-side controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=realtime)
for more information.

```javascript
import WebSocket from "ws";

const callId = "rtc_u1_9c6574da8b8a41a18da9308f4ad974ce";
const ws = new WebSocket(`wss://api.openai.com/v1/realtime?call_id=rtc_u1_9c6574da8b8a41a18da9308f4ad974ce`, {
  headers: {
    Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
  },
});

ws.on("open", () => {
  ws.send(
    JSON.stringify({
      type: "response.create",
    })
  );
});
```


## Redirect the call

Transfer an active call using the
[Refer call endpoint](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/refer). Provide the
`call_id` as well as the `target_uri` that should be placed in the SIP `Refer-To`
header (for example `tel:+14155550123` or `sip:agent@example.com`).

```bash
curl -X POST "https://api.openai.com/v1/realtime/calls/$CALL_ID/refer" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target_uri": "tel:+14155550123"}'
```


OpenAI returns `200 OK` once the REFER is relayed to your SIP provider. The
downstream system handles the rest of the call flow for the caller.

## Hang up the call

End the session with the [Hang up endpoint](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/hangup)
when your application should disconnect the caller. This endpoint can be used to
terminate both SIP and WebRTC realtime sessions.

```bash
curl -X POST "https://api.openai.com/v1/realtime/calls/$CALL_ID/hangup" \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```


The API responds with `200 OK` when it starts tearing down the call.

<a id="dedicated-sip-ip-ranges"></a>

## SIP signaling and media IP ranges

Realtime SIP calls use separate network paths for signaling and media. To ensure proper operation,
configure your network to allow signaling and media traffic as described below.

### SIP signaling

`sip.api.openai.com` and `sip-eu.api.openai.com` are GeoIP-routed endpoints. Your network must allow
outbound TCP/TLS traffic to the addresses returned by DNS on port `5061`.

### SRTP media

The API specifies a separate media IP address and UDP port in the negotiated SDP. Your network must
allow bidirectional SRTP traffic over UDP to and from the following CIDRs:

- `13.79.45.80/28`
- `23.98.140.64/28`
- `40.67.149.176/28`
- `40.83.204.240/28`

## Server examples

The following is an example of a `realtime.call.incoming` handler. It accepts the call and then logs all the events from
the Realtime API.

For the Ruby example, set the `OPENAI_API_KEY` and `OPENAI_WEBHOOK_SECRET`
environment variables, then install the required dependencies with
`gem install openai webrick async-websocket`.

Handle an incoming SIP call

```python
from flask import Flask, request, Response, jsonify, make_response
from openai import OpenAI, InvalidWebhookSignatureError
import asyncio
import json
import os
import requests
import time
import threading
import websockets

app = Flask(__name__)
client = OpenAI(webhook_secret=os.environ["OPENAI_WEBHOOK_SECRET"])

AUTH_HEADER = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}

call_accept = {
    "type": "realtime",
    "instructions": "You are a support agent.",
    "model": "gpt-realtime-2.1",
}

response_create = {
    "type": "response.create",
    "response": {
        "instructions": ("Say to the user 'Thank you for calling, how can I help you'")
    },
}


async def websocket_task(call_id):
    try:
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?call_id=" + call_id,
            additional_headers=AUTH_HEADER,
        ) as websocket:
            await websocket.send(json.dumps(response_create))

            while True:
                response = await websocket.recv()
                print(f"Received from WebSocket: {response}")
    except Exception as e:
        print(f"WebSocket error: {e}")


@app.route("/", methods=["POST"])
def webhook():
    try:
        event = client.webhooks.unwrap(request.data, request.headers)

        if event.type == "realtime.call.incoming":
            requests.post(
                "https://api.openai.com/v1/realtime/calls/"
                + event.data.call_id
                + "/accept",
                headers={**AUTH_HEADER, "Content-Type": "application/json"},
                json=call_accept,
            )
            threading.Thread(
                target=lambda: asyncio.run(websocket_task(event.data.call_id)),
                daemon=True,
            ).start()
            return Response(status=200)
    except InvalidWebhookSignatureError as e:
        print("Invalid signature", e)
        return Response("Invalid signature", status=400)


if __name__ == "__main__":
    app.run(port=8000)
```

```ruby
require "openai"
require "webrick"

client = OpenAI::Client.new(webhook_secret: ENV.fetch("OPENAI_WEBHOOK_SECRET"))
server = WEBrick::HTTPServer.new(
  BindAddress: "127.0.0.1",
  Port: Integer(ENV.fetch("OPENAI_WEBHOOK_PORT", "8000")),
  Logger: WEBrick::Log.new($stderr, WEBrick::BasicLog::WARN),
  AccessLog: []
)
sideband_workers = []

server.mount_proc("/webhook") do |request, response|
  if request.request_method != "POST"
    response.status = 405
    next
  end

  headers = request.header.transform_values(&:first)
  event = client.webhooks.unwrap(request.body, headers)

  if event.is_a?(OpenAI::Models::Webhooks::RealtimeCallIncomingWebhookEvent)
    call_id = event.data.call_id
    sideband_workers.select!(&:alive?)
    sideband_workers << Thread.new(call_id) do |active_call_id|
      client.realtime.calls.accept(
        active_call_id,
        type: :realtime,
        model: "gpt-realtime-2.1",
        instructions: "You are a helpful support agent."
      )

      client.realtime.connect_to_call(call_id: active_call_id) do |connection|
        connection.response.create(
          instructions: "Thank the caller and ask how you can help."
        )
        connection.each do |server_event|
          puts "Realtime event: #{server_event.type}"
        end
      end
    end
  end

  response.status = 200
  response.body = "ok"
rescue OpenAI::Errors::InvalidWebhookSignatureError, ArgumentError
  response.status = 400
  response.body = "Invalid signature"
ensure
  server.shutdown if ENV["OPENAI_WEBHOOK_EXIT_AFTER_REQUEST"] == "1"
end

Signal.trap("INT") do
  sideband_workers.each(&:kill)
  server.shutdown
end
port = server.listeners.first.addr[1]
puts "Webhook server listening on http://127.0.0.1:#{port}/webhook"
$stdout.flush
server.start
sideband_workers.each(&:join)
```


## Next steps

Now that you've connected over SIP, use the left navigation or click into these pages to start building your realtime application.

- [Realtime prompting guide](https://developers.openai.com/api/docs/guides/voice-prompting)
- [Managing conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Webhooks and server-side controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=realtime)
- [Managing costs](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=realtime)
- [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)

### Additional Resources

- [JavaScript demo](https://hello-realtime.val.run/)
- [Connect the Realtime SIP Connector to Twilio Elastic SIP Trunking](https://www.twilio.com/en-us/blog/developers/tutorials/product/openai-realtime-api-elastic-sip-trunking)