# Twilio ConversationRelay setup

This guide connects a Twilio voice number to the CallFlow FastAPI WebSocket. Run
the credential-free simulator first; it is the quickest way to confirm the domain
logic before adding telephony.

## 1. Prerequisites

- A Twilio account and a voice-capable Twilio number.
- ConversationRelay enabled for the account. Twilio's current onboarding flow
  requires reviewing and accepting its Predictive and Generative AI/ML Features
  Addendum in the Voice settings.
- A public deployment or secure development tunnel that serves both `https://`
  and `wss://` with a trusted certificate.
- A human-agent phone number in E.164 format if you want live handoff.
- A Groq API key for natural live LLM responses. It is not needed for the local
  browser simulator.

Use Twilio's official [ConversationRelay onboarding guide](https://www.twilio.com/docs/voice/conversationrelay/onboarding)
as the authority if Console labels or eligibility requirements differ.

## 2. Configure the application

Create a local environment file:

```powershell
Copy-Item .env.example .env
```

Set the following values in `.env`:

```dotenv
PUBLIC_BASE_URL=https://voice.example.com
PUBLIC_WS_URL=wss://voice.example.com/ws/conversation
TWILIO_AUTH_TOKEN=replace_with_your_auth_token
VALIDATE_TWILIO_SIGNATURE=true
HUMAN_AGENT_PHONE=+15551234567
GROQ_API_KEY=gsk_replace_with_your_own_key
GROQ_MODEL=openai/gpt-oss-20b
API_KEY=replace_with_a_long_random_dashboard_secret
```

Important:

- Do not add a trailing slash to `PUBLIC_BASE_URL`.
- `PUBLIC_WS_URL` must use `wss://`, not `https://`.
- The URLs must be exactly the public URLs Twilio requests. Signature validation
  can fail if a proxy changes the scheme, host, path, query string, or encoding.
- Store all real values in a deployment secret manager. Never commit `.env`.

The application needs the Auth Token only to validate signed inbound Twilio
requests. Do not use a Twilio API key or Account SID in its place.

## 3. Start and expose the service

```powershell
docker compose up --build
```

For local development, expose port `8001` using a reputable HTTPS tunnel. For
example, if you already use ngrok:

```powershell
ngrok http 8001
```

Copy the generated HTTPS origin into `PUBLIC_BASE_URL`, convert the scheme to
`wss` for `PUBLIC_WS_URL`, restart CallFlow, and keep the tunnel URL stable for the
test. A free tunnel URL may change between sessions.

Verify the public health route before configuring Twilio:

```text
https://your-public-host/api/v1/health
```

## 4. Configure the Twilio number

In the Twilio Console:

1. Open the voice-capable phone number.
2. In its incoming-call configuration, choose **Webhook**.
3. Set the request method to **HTTP POST**.
4. Set the voice webhook to:

   ```text
   https://your-public-host/voice/incoming
   ```

5. Save the number configuration.

An alternative is to create a TwiML App whose Voice URL points to the same route,
then associate that app with the number. Both approaches should produce TwiML with
`<Connect><ConversationRelay>`.

## 5. What CallFlow returns

`POST /voice/incoming` returns a ConversationRelay session configured for:

| Setting | Value | Purpose |
|---|---|---|
| WebSocket | `PUBLIC_WS_URL` | Connect Twilio to FastAPI |
| Transcription provider | `Deepgram` | Streaming speech recognition |
| Speech model | `flux` | Semantic end-of-turn behavior |
| Transcription language | `multi` | Detect supported caller language |
| TTS provider | `ElevenLabs` | Natural response speech |
| TTS language | `multi` | Speak the language of response text |
| Partial prompts | enabled | React to eager/unfinished turn signals |
| Speech interruption | enabled | Let the caller interrupt output |
| Preemption | enabled | Let a later response supersede old output |

Provider, model, language, voice, and advanced attribute combinations can change.
Check the current [ConversationRelay TwiML reference](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay)
before production. Twilio ends a session when an invalid combination is supplied.

Twilio provides Deepgram and ElevenLabs within ConversationRelay for this design;
CallFlow does not ask you for their direct API keys. Availability and charges are
governed by your Twilio account and current Twilio terms.

## 6. Place a test call

1. Watch the application logs.
2. Call the Twilio number from a permitted destination.
3. Confirm the welcome disclosure plays.
4. Say: “I want to book a consultation tomorrow afternoon.”
5. Interrupt the response once to demonstrate barge-in.
6. Say: “Please connect me to a person.”
7. Confirm the configured human number rings.
8. Answer it and verify the whisper plays before the caller is bridged.

The initial WebSocket message should be `setup`; caller turns arrive as `prompt`
messages. CallFlow responds with ConversationRelay `text` token messages. See the
official [WebSocket message guide](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages)
for the protocol.

## 7. Warm handoff details

CallFlow ends the ConversationRelay session with an `end` message containing a
small JSON-encoded `handoffData` string. Twilio posts that value to:

```text
https://your-public-host/voice/connect-action
```

The callback returns `<Dial answerOnBridge="true">` with a nested `<Number>` whose
`url` points to:

```text
https://your-public-host/voice/agent-whisper
```

The whisper gives the human a short reason and summary while the original caller
continues to hear ringing. This pattern follows Twilio's documented
[call-whisper behavior](https://www.twilio.com/docs/glossary/call-whisper).

The `<Dial>` also posts its result to `/voice/dial-action`. A completed human leg
is recorded as `handoff_completed`; busy, failed, cancelled, and no-answer results
create an idempotent callback ticket before the fallback message is played.

Do not put payment-card data or other sensitive information in `handoffData`,
TwiML attributes, or custom ConversationRelay parameters. Twilio specifically
notes that these fields are not treated as PCI-data fields.

## 8. Request validation

Twilio signs HTTP webhooks and the ConversationRelay WebSocket handshake with the
`X-Twilio-Signature` header. CallFlow validates the signature with
`TWILIO_AUTH_TOKEN` when `VALIDATE_TWILIO_SIGNATURE=true`.

Keep validation enabled for every public deployment. The only normal reason to
set it to `false` is local simulator work where Twilio never reaches the service.
Do not rely on IP allow-lists as a replacement; follow Twilio's
[webhook security guidance](https://www.twilio.com/docs/usage/webhooks/webhooks-security).

## 9. Optional n8n follow-up

1. Start n8n locally with `docker compose --profile automation up --build`, or use
   your hosted n8n instance.
2. Import the workflow in `n8n/workflows/`.
3. Review the nodes and replace all example destinations.
4. Activate the workflow.
5. Copy its **production** Webhook URL into `N8N_POST_CALL_WEBHOOK`.
6. Restart CallFlow and complete one simulator or live session.

Use n8n's [Webhook node documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/)
for test-versus-production URL behavior. Treat the endpoint as a secret, validate
the payload, and add authentication appropriate to your deployment.

## Troubleshooting

### Twilio cannot connect to the WebSocket

- Confirm `PUBLIC_WS_URL` starts with `wss://` and is reachable publicly.
- Confirm the certificate is trusted and not self-signed.
- Confirm the WebSocket path is exactly `/ws/conversation`.
- Check the Twilio Debugger and application logs for ConversationRelay errors.

### Every webhook is rejected

- Confirm `TWILIO_AUTH_TOKEN` belongs to the Twilio account receiving the call.
- Confirm `PUBLIC_BASE_URL` matches the externally requested origin exactly.
- Ensure a reverse proxy forwards the original host and scheme consistently.
- Do not validate a reconstructed internal URL such as `http://callflow:8001`.

### The call connects but the model sounds scripted

- Check `/api/v1/health` to see whether Groq is configured.
- Confirm `GROQ_API_KEY` is available to the running container, then restart it.
- Confirm the configured model is currently available in
  [Groq's model list](https://console.groq.com/docs/models).

### Handoff does not ring

- Set `HUMAN_AGENT_PHONE` in full E.164 form, including `+` and country code.
- Confirm your Twilio project is permitted to call that destination.
- For trial accounts, confirm the destination satisfies current trial restrictions.
- Inspect the `<Connect>` action request and Twilio Debugger.

### n8n receives no post-call event

- Use the activated workflow's production URL, not an expired test URL.
- Confirm the n8n endpoint is reachable from the CallFlow container/deployment.
- Remember delivery is best effort in the current implementation; inspect app and
  n8n logs, then replay a new sandbox session.

## Launch checklist

- [ ] AI disclosure and legally required consent reviewed
- [ ] ConversationRelay eligibility/addendum completed
- [ ] Public HTTPS and WSS endpoints use trusted certificates
- [ ] Twilio HTTP and WebSocket signature validation passes
- [ ] Groq key stored outside source control
- [ ] Dashboard API key changed from the local example
- [ ] Human handoff and unanswered-call fallback tested
- [ ] Sensitive-data redaction and retention policy implemented
- [ ] Current Twilio/Groq provider and model settings verified
- [ ] Alerts, backups, rate limits, and durable post-call retries added
