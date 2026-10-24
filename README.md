# CallFlow AI

Real-time voice agent. CallFlow answers a Twilio phone call, receives speech events through
ConversationRelay, reasons with a low-latency Groq model, executes controlled
business tools, and transfers sensitive or complex calls to a human with context.

**Hosted browser sandbox:** <https://callflow-voice-ai.vercel.app/>

The included browser sandbox runs without Twilio, Groq, Deepgram, ElevenLabs,
or n8n credentials, enabling local evaluation and integration testing.

## What the hosted website does

The Vercel website serves the static browser sandbox from `dist/`. It lets a user
type or speak a caller request and observe intent routing, deterministic tool
results, session memory, dashboard metrics, and a simulated warm handoff. When
supported, the browser's speech-recognition API fills and submits the caller turn,
and browser speech synthesis reads Ava's reply aloud. Text input always remains as
the fallback. The browser's speech service may process microphone audio; CallFlow
receives only the resulting transcript and does not record or store audio. Speech
processing availability and behavior depend on the visitor's browser. Call records
and KPI values in this sandbox are synthetic.

The browser voice controls are an interview-friendly wrapper around the sandbox;
they are not Twilio telephony and do not use Deepgram or ElevenLabs. The live phone
architecture described below remains a separate, credentialed backend path.

The optional **Caller number** field only labels and masks the simulated caller in
the browser session. Entering a phone number does **not** dial it, send an SMS, or
start a real Twilio call. The hosted sandbox performs no outbound telephony.

## How live phone calls work

CallFlow's telephony backend is designed for **inbound** calls:

1. A customer calls a voice-enabled Twilio number.
2. Twilio sends `POST /voice/incoming` and opens ConversationRelay.
3. Deepgram transcribes the caller; FastAPI receives structured prompt events over
   `/ws/conversation`.
4. The orchestration layer selects a controlled business tool and Groq streams a
   concise response; ElevenLabs speaks it through Twilio.
5. When escalation is required, Twilio calls the configured
   `HUMAN_AGENT_PHONE`, plays a private summary to the human, and bridges the call.
6. The final outcome is stored and can be sent to n8n for CRM, notification, or
   follow-up automation.

Only the human-handoff step dials a configured agent number. Outbound customer
calling or campaign dialing is not implemented in this repository.


## Architecture

```text
Caller
  |
  v
Twilio Programmable Voice
  |
  +-- ConversationRelay
      |  Deepgram Flux STT + turn detection
      |  ElevenLabs multilingual TTS
      v
FastAPI /ws/conversation
  |
  +-- session memory + deterministic intent guardrails
  +-- Groq streaming LLM (optional in the simulator)
  +-- controlled tools
  |     customer lookup | appointment | ticket
  +-- SQLite call record --> dashboard
  +-- post-call event -----> n8n workflow
  |
  +-- end + handoffData --> /voice/connect-action
                               |
                               +-- <Dial><Number url="agent whisper">
                                                   |
                                                   v
                                              Human agent
```

See [docs/architecture.md](docs/architecture.md) for message flow, trust
boundaries, failure behavior, and production considerations.

## Quick start: credential-free simulator

### Option A: Docker

Requirements: Docker Desktop with Docker Compose.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open <http://localhost:8001/?key=local-demo-key>. No external account or API key
is required for this path.

### Option B: Python

Requirements: Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload --port 8001
```

Open <http://localhost:8001/?key=local-demo-key>, or call the simulator directly:

```powershell
$headers = @{ "X-API-Key" = "local-demo-key" }
$body = @{
  session_id = "callflow-sandbox"
  caller = "+923001234567"
  text = "I need to book a consultation tomorrow afternoon"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://localhost:8001/api/v1/simulate `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

Use the same `session_id` for later turns so the simulator preserves context.

## Do I need a Groq API key?

Not for the local browser sandbox. Without `GROQ_API_KEY`, the simulator and
tests use a deterministic fallback, and the appointment, ticket, customer, and
handoff tools still work.

For the intended live experience, add a Groq key so the agent can generate
natural, context-aware responses while streaming. Put it only in your local
`.env` or deployment secret store:

```dotenv
GROQ_API_KEY=gsk_replace_with_your_own_key
GROQ_MODEL=openai/gpt-oss-20b
```

Never paste a real key into chat, source code, screenshots, Git commits, or n8n
workflow JSON. The selected model is configurable because hosted model catalogs
and availability can change; check the current [Groq supported models](https://console.groq.com/docs/models)
before production deployment.

## Live Twilio setup

Live calls require a Twilio account, a voice-capable number, a public HTTPS/WSS
deployment, ConversationRelay access, and a human-agent number for transfers.
Follow [docs/twilio-setup.md](docs/twilio-setup.md) from start to finish.

The live voice configuration uses:

- Deepgram as the ConversationRelay transcription provider.
- `flux` as the speech model for semantic turn detection.
- ElevenLabs as the multilingual TTS provider.
- Short streamed text chunks so Twilio can begin speaking before the full answer
  is complete.
- Speech interruption, partial prompts, backchannel handling, and preemption for
  natural turn taking.

Twilio provides the STT/TTS layer through ConversationRelay; this application
does not call Deepgram or ElevenLabs APIs directly. Review provider availability,
billing, data handling, and eligibility in your own Twilio account before launch.

## Warm human handoff

When the caller asks for a person, repeats a failed request, or reaches an
escalation rule, the WebSocket sends a ConversationRelay `end` message with only
a record identifier and reason. Twilio then calls `/voice/connect-action`.
The handler returns `<Dial answerOnBridge="true">` and a `<Number>` with
`/voice/agent-whisper`, so the human hears the stored reason and summary before
being bridged to the caller. `/voice/dial-action` records whether the human leg
completed or creates an idempotent callback ticket on busy, failed, or no-answer.

If `HUMAN_AGENT_PHONE` is empty or the agent does not answer, the call follows a
clear callback fallback instead of silently failing.

## Optional n8n post-call automation

Set `N8N_POST_CALL_WEBHOOK` to an n8n production Webhook URL. At call completion,
CallFlow makes a best-effort JSON POST containing the call outcome and summary.
Use the included workflow as a starting point for:

- CRM activity creation;
- appointment confirmation;
- support-ticket notification;
- human follow-up queues; or
- analytics enrichment.

Start the optional local n8n service with:

```powershell
docker compose --profile automation up --build
```

Then open <http://localhost:5678>. Import the workflow from `n8n/workflows/`,
review every node, activate it, and place its production webhook URL in `.env`.

## API surface

| Method | Route | Purpose | Authentication |
|---|---|---|---|
| `GET` | `/` | Operations dashboard and simulator | `?key=...` for dashboard API calls |
| `GET` | `/api/v1/health` | Readiness and integration status | Public |
| `POST` | `/api/v1/simulate` | Credential-free text conversation | `X-API-Key` |
| `GET` | `/api/v1/calls` | Recent call and simulator sessions | `X-API-Key` |
| `GET` | `/api/v1/metrics` | Dashboard KPIs | `X-API-Key` |
| `POST` | `/voice/incoming` | Return ConversationRelay TwiML to Twilio | Twilio signature |
| `WS` | `/ws/conversation` | ConversationRelay event and token stream | Twilio signature |
| `POST` | `/voice/connect-action` | Resume TwiML after relay ends | Twilio signature |
| `POST` | `/voice/agent-whisper` | Brief the answering human | Twilio signature |
| `POST` | `/voice/dial-action` | Record transfer result or save callback | Twilio signature |

`API_KEY=local-demo-key` is intentionally convenient for local evaluation. Change
it to a long random secret in every deployed environment. Do not expose the
simulator or dashboard APIs publicly with the example value.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The CI workflow compiles the application, checks installed dependencies, and runs
the test suite on supported Python versions.

## Safety and production checklist

- Clearly disclose that callers are speaking with an AI system.
- Obtain any consent required for transcription, recording, or automated
  processing in the caller's jurisdiction.
- Keep payment-card data, passwords, one-time codes, health data, and other
  sensitive information out of prompts, logs, custom parameters, and handoff data.
- Keep Twilio request validation enabled in production and terminate TLS at a
  trusted public endpoint.
- Use a managed database, centralized logs with redaction, queue/retry handling,
  rate limiting, backups, and a secret manager before handling real customers.
- Keep consequential or regulated decisions and irreversible actions under human
  review. The included tools operate on synthetic data only.

## Repository map

```text
app/                    FastAPI transport, orchestration, tools, storage, dashboard
docs/                   Architecture, Twilio setup, and operations walkthrough
n8n/workflows/          Importable post-call automation
sample/                 Example events and simulator conversations
tests/                  Unit and integration tests
.github/workflows/      CI checks
```

## Official references

- [Twilio ConversationRelay](https://www.twilio.com/docs/voice/conversationrelay)
- [ConversationRelay TwiML reference](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay)
- [ConversationRelay WebSocket messages](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages)
- [Groq streaming text generation](https://console.groq.com/docs/text-chat)
- [n8n Webhook node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/)

## License

MIT — see [LICENSE](LICENSE).
