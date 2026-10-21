# Five-minute interview demo

This script demonstrates the system without spending telephony or LLM credits.
Use the live-call extension only when your Twilio and Groq environment is ready.

## Before the interview

```powershell
Copy-Item .env.example .env
docker compose up --build -d
python -m unittest discover -s tests -v
```

Open <http://localhost:8001/?key=local-demo-key> in a clean browser window. Leave
the architecture document and terminal ready in separate tabs. Do not show `.env`
or any provider dashboard containing credentials.

## 0:00–0:40 — Frame the business problem

Say:

> CallFlow AI is a real-time phone agent for service businesses. It can identify a
> caller, book an appointment, create a support ticket, and bring a human into the
> call with context. I designed the browser simulator so the whole orchestration can
> be reviewed without paid credentials.

Point to the KPI cards and the empty recent-call list. Mention that the same domain
logic is used by the simulator and the live Twilio WebSocket.

## 0:40–2:20 — Run one useful conversation

In the simulator, set **Caller** to the synthetic fixture `+15550101001`, keep one
session, and send these turns:

1. `Please look up my customer account.`
2. `Please book a consultation tomorrow afternoon.`
3. Provide any missing detail the agent asks for.

After each turn, point out:

- the detected intent and action metadata;
- that customer and appointment actions come from deterministic tools;
- that a confirmation is shown only after validated tool execution; and
- that session memory keeps the turns connected.

The phone maps to synthetic record `CUST-DEMO-001`, and the tool response exposes
only a masked phone number. The interview point is the controlled tool boundary,
not the fixture itself.

## 2:20–3:10 — Show the safety path

Start a new session and send:

```text
My issue is urgent and I need to speak with a human agent now.
```

Show the handoff outcome and explain:

> An explicit request for a person bypasses model persuasion. In a real call, the
> WebSocket sends ConversationRelay's end message with a small handoff reference.
> Twilio resumes at the Connect action, dials the human with answer-on-bridge, and
> plays a private whisper containing the reason and short summary.

Emphasize that sensitive data is not placed in `handoffData`.

## 3:10–4:00 — Explain real-time design

Open `docs/architecture.md` and cover four decisions:

1. Twilio ConversationRelay supplies Deepgram Flux STT/turn detection and
   ElevenLabs multilingual TTS.
2. FastAPI handles structured WebSocket events rather than raw audio frames.
3. Groq response deltas are forwarded in short chunks, reducing time to first
   spoken output.
4. Business tools remain deterministic and validated even when an LLM is enabled.

Say that the missing-key fallback is deliberate: reviewers can run the project,
while live deployment gets natural LLM wording by adding a Groq secret.

## 4:00–4:35 — Show operational completeness

Return to the dashboard and refresh it. Show the updated metrics and recent-call
record. Briefly mention:

- SQLite for a portable demo, with a managed database as the production upgrade;
- signed Twilio HTTP and WebSocket requests;
- best-effort n8n post-call events, with an outbox/retry worker as the production
  reliability upgrade; and
- Docker plus CI tests for repeatable evaluation.

## 4:35–5:00 — Close with ownership

Say:

> I treated the LLM as one component, not the whole product. The project covers the
> voice protocol, latency, safe tools, state, human escalation, automation,
> security, observability, deployment, and a no-credential test path.

Then ask whether the interviewer would like to inspect the WebSocket handler, tool
registry, or live TwiML handoff.

## Optional live-call extension

Only use this after completing `docs/twilio-setup.md`.

1. Call the configured Twilio number.
2. Ask for an appointment.
3. Interrupt the assistant once to demonstrate barge-in.
4. Switch briefly between English and a supported second language.
5. Ask for a human.
6. Answer the agent phone and let the interviewer hear the private whisper before
   the bridge completes.

Do not improvise payment-card, health, password, or one-time-code examples.

## Technical questions you may be asked

### Why not let the LLM directly execute everything?

The model is useful for intent and natural phrasing, but business writes require
validation, authorization, idempotency, and reliable outcomes. The tool registry
is an allow-list and the application confirms only verified results.

### Why use ConversationRelay instead of handling raw audio?

It keeps the portfolio focused on the agent and business orchestration while
Twilio handles the live STT/TTS session. The application still demonstrates a
real-time WebSocket protocol, interruptions, partial turns, and token streaming.

### What makes the handoff “warm”?

The human is briefed before the caller is bridged. The transfer includes a reason
and a short stored-call summary, so the caller does not have to restart the story.

### What happens when Groq is down?

The application catches provider errors, gives a short safe fallback, keeps
deterministic tools available, and offers human escalation. A live call is not left
waiting indefinitely for a model response.

### What would you change for production scale?

Use a managed relational database, shared session state, a durable event outbox,
workers with retries, centralized redacted logs, distributed tracing, rate limits,
secret management, and load testing focused on concurrent WebSockets and first
token latency.

### How would you measure quality?

Track task completion, containment, handoff rate, tool failure rate, correction
turns, interruption recovery, first-response latency, first-token latency, and
caller drop-off. Review samples with a human rubric instead of trusting one LLM
judge score.

## Resume version

> Built a real-time voice automation platform using FastAPI WebSockets, Twilio
> ConversationRelay, Deepgram Flux, ElevenLabs TTS, Groq streaming inference, safe
> appointment/support tools, context-preserving human handoff, n8n automation,
> Docker, CI, and an analytics dashboard.
