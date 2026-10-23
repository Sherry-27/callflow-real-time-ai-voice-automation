# Architecture

CallFlow AI is a real-time voice orchestration platform. Twilio owns the
phone connection and speech layer, while the FastAPI application owns dialogue
state, model calls, business rules, tools, persistence, and escalation.

## Design goals

1. Start speaking quickly without waiting for a complete LLM response.
2. Allow callers to interrupt naturally and recover from partial turns.
3. Keep business mutations deterministic, validated, and auditable.
4. Make human escalation explicit, contextual, and failure-safe.
5. Remain demonstrable without paid credentials.

## Components

| Component | Responsibility |
|---|---|
| Twilio Programmable Voice | Receive the phone call and execute TwiML |
| ConversationRelay | Convert speech to prompt events and response text to speech |
| Deepgram Flux | ConversationRelay STT model and semantic end-of-turn detection |
| ElevenLabs | ConversationRelay multilingual text-to-speech provider |
| FastAPI | HTTP webhooks, WebSocket protocol, simulator, and dashboard APIs |
| Session orchestrator | Normalize events, retain short context, choose actions, stream replies |
| Groq | Generate concise, natural response text as a stream |
| Tool registry | Execute allow-listed customer, appointment, and ticket functions |
| SQLite store | Persist demo calls, transcripts, outcomes, and dashboard metrics |
| n8n | Receive optional post-call events and continue business automation |

ConversationRelay is the STT/TTS integration point. The application exchanges
structured prompt events and text tokens; it does not handle raw telephony audio
or hold separate Deepgram and ElevenLabs API credentials.

## Live call sequence

```text
Twilio          HTTP API              WebSocket agent          Tools / Groq
   |                |                        |                       |
   | POST /voice/incoming                    |                       |
   |--------------->|                        |                       |
   | <Connect><ConversationRelay>            |                       |
   |<---------------|                        |                       |
   |================ setup =================>| create session        |
   |================ prompt ================>| normalize final turn  |
   |                |                        |-- decide_action ------>|
   |                |                        |<- verified result -----|
   |                |                        |-- stream request ------>|
   |<=============== text tokens ============|<- token deltas --------|
   |                |                        | persist turn           |
```

Twilio sends a `setup` message after the WebSocket connects. Final `prompt`
messages become user turns. Partial prompts support responsive interruption logic
but are not committed as completed user turns until the protocol marks them final.
The agent sends short `text` token messages back so TTS can start early.

## Conversation and tool policy

The orchestration layer classifies each caller request before generation. The
model helps phrase responses; it is not trusted as the source of record.

The allow-listed tool layer provides deterministic functions such as:

- `lookup_customer` — reads seeded demo customer data;
- `book_appointment` — validates required details before storing a booking;
- `create_ticket` — creates a categorized support record; and
- `request_handoff` — records why a human is needed.

Tool arguments are validated by application code, and tool output is passed back
to the response generator as verified context. This prevents a fluent model answer
from being mistaken for a confirmed booking or real customer record.

## Natural turn taking

The generated `<ConversationRelay>` uses Deepgram with `speechModel="flux"`,
partial prompts, speech interruption, backchannel filtering, and preemptible output.
ElevenLabs multilingual TTS is selected with `ttsLanguage="multi"`. These controls
are session-level TwiML configuration; supported combinations must be checked
against the current [ConversationRelay TwiML reference](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay).

The WebSocket also tracks interrupted responses and caller frustration. A caller
can always ask for a person. Repeated failure or an explicit escalation intent
bypasses further model persuasion and starts handoff.

## Warm handoff sequence

```text
Agent WebSocket           Twilio                 Human number
      |                      |                         |
      | end + handoffData    |                         |
      |--------------------->|                         |
      |                      | POST /voice/connect-action
      |                      |------------------------> HTTP API
      |                      |<-- <Dial><Number url=whisper>
      |                      |-------------------------| rings
      |                      |        whisper request  |
      |                      |<------------------------|
      |                      |-- summary + reason ---->|
      |                      |==== bridge caller ======|
```

The WebSocket sends the official ConversationRelay `end` message and encodes a
small JSON object as the string `handoffData`. Control returns to the `<Connect>`
action callback. The callback parses only expected fields, loads the stored call,
and returns `<Dial answerOnBridge="true">`. The nested `<Number>` points to the
agent-whisper endpoint, so the answering human hears context while the caller
continues to hear ringing. The `<Dial action>` callback receives `DialCallStatus`;
it records a completed handoff or creates a real, idempotent callback ticket before
telling an unanswered caller that follow-up was saved.

Twilio explicitly warns that `handoffData` is not treated as a PCI-data field.
CallFlow therefore puts only a short reason and record identifier there; sensitive
information belongs in a protected system of record, not TwiML or handoff payloads.

## Credential-free simulator

`POST /api/v1/simulate` calls the same domain decision and tool layers used by a
live session, but uses text input instead of ConversationRelay events. When Groq
is absent, a deterministic response renderer explains the verified action result.
This path intentionally needs no external account, network call, or paid service.

The optional caller number is metadata for the simulated session only. Supplying
it never initiates a call or SMS. Live telephony begins only when a customer calls
the configured Twilio number; this repository does not implement an outbound
customer dialer.

The simulator API still requires `X-API-Key`. This protects a deployed demo from
unrestricted writes; `local-demo-key` is only a local default and must be replaced.

## Post-call automation

After completion, the application saves the final outcome and attempts one JSON
POST to `N8N_POST_CALL_WEBHOOK`. Delivery is best effort so an automation outage
does not hold a phone call open. A production design should write an outbox event
inside the same database transaction, then let a worker retry with exponential
backoff, idempotency keys, and a dead-letter queue.

## Trust boundaries

```text
Untrusted                  Validated application boundary         Trusted action
---------                  ------------------------------         --------------
Caller speech   ---------> normalized prompt + policy checks
LLM suggestion  ---------> schema / tool allow-list       ------> local tool
Twilio webhook  ---------> X-Twilio-Signature validation  ------> TwiML response
Dashboard input ---------> X-API-Key + field validation    ------> demo database
n8n URL         <--------- redacted, minimal post-call event
```

The Twilio signature must be calculated against the exact public URL and all form
parameters. A reverse proxy must preserve the configured external URL. The
WebSocket handshake has its own `X-Twilio-Signature` header and is rejected before
acceptance if validation fails.

## Failure behavior

| Failure | Behavior |
|---|---|
| Groq key missing | Deterministic local response; tools still operate |
| Groq timeout/error | Short apology and offer of human assistance |
| Unknown/malformed WebSocket event | Ignore safely; do not mutate state |
| Invalid Twilio signature | Reject HTTP request or WebSocket handshake |
| Human number missing | Save callback request and explain the fallback |
| Human does not answer | Save an idempotent callback ticket, then give a clear message |
| n8n unavailable | Record remains stored; call completion is not blocked |
| Database/tool validation failure | No false confirmation; offer retry or handoff |

## Data and privacy

The bundled SQLite store is intended for local and low-volume sandbox use, not regulated or high-volume traffic.
Before production:

- minimize transcript retention and define a deletion schedule;
- encrypt storage and backups;
- redact phone numbers and secrets from logs;
- separate operational metrics from transcript content;
- disclose AI use and obtain legally required consent;
- perform provider and regional data-processing review; and
- use human approval for consequential or irreversible actions.

## Scaling path

For multiple application replicas, replace in-process live-session assumptions
with shared ephemeral state, move SQLite to a managed relational database, and
publish post-call work through a durable queue. Use sticky routing only as a short
transition; a dropped WebSocket should not lose the durable call record. Add
distributed tracing for webhook, model-first-token, tool, and TTS milestones.

## Source references

- [ConversationRelay overview](https://www.twilio.com/docs/voice/conversationrelay)
- [ConversationRelay onboarding](https://www.twilio.com/docs/voice/conversationrelay/onboarding)
- [WebSocket message schemas](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages)
- [Twilio webhook security](https://www.twilio.com/docs/usage/webhooks/webhooks-security)
- [Twilio call whisper](https://www.twilio.com/docs/glossary/call-whisper)
- [Groq text streaming](https://console.groq.com/docs/text-chat)
- [Groq local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling)
