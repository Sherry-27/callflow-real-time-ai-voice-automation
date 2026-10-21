"""Small, dependency-free TwiML builders with XML escaping."""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr


def _xml(value: str) -> str:
    return escape(value or "")


def conversation_relay_twiml(
    websocket_url: str,
    action_url: str,
    caller: str = "unknown",
) -> str:
    """Connect an inbound call to multilingual, interruption-aware speech."""
    greeting = (
        "Hello, you are speaking with CallFlow AI. I can help with appointments "
        "and support, or connect you to a person at any time. How may I help?"
    )
    attributes = {
        "url": websocket_url,
        "welcomeGreeting": greeting,
        "welcomeGreetingInterruptible": "any",
        "ttsLanguage": "multi",
        "ttsProvider": "ElevenLabs",
        "transcriptionLanguage": "multi",
        "transcriptionProvider": "Deepgram",
        "speechModel": "flux",
        "eotThreshold": "0.75",
        "partialPrompts": "true",
        "interruptible": "any",
        "interruptSensitivity": "medium",
        "reportInputDuringAgentSpeech": "any",
        "ignoreBackchannel": "true",
        "preemptible": "true",
        "dtmfDetection": "true",
        "events": "speaker-events tokens-played",
        "elevenlabsTextNormalization": "off",
    }
    rendered = " ".join(f"{name}={quoteattr(value)}" for name, value in attributes.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect action={quoteattr(action_url)} method="POST">'
        f"<ConversationRelay {rendered}>"
        f'<Parameter name="caller" value={quoteattr(caller)} />'
        '<Parameter name="channel" value="phone" />'
        "</ConversationRelay>"
        "</Connect>"
        "</Response>"
    )


def warm_handoff_twiml(
    agent_phone: str,
    whisper_url: str,
    dial_action_url: str,
    fallback_message: str,
) -> str:
    if not agent_phone:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"<Say>{_xml(fallback_message)}</Say>"
            "<Hangup/>"
            "</Response>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>Please hold while I bring a specialist into the call.</Say>"
        f'<Dial answerOnBridge="true" timeout="25" '
        f'action={quoteattr(dial_action_url)} method="POST">'
        f'<Number url={quoteattr(whisper_url)} method="POST">{_xml(agent_phone)}</Number>'
        "</Dial>"
        "</Response>"
    )


def call_result_twiml(message: str = "") -> str:
    say = f"<Say>{_xml(message)}</Say>" if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Hangup/></Response>'


def agent_whisper_twiml(summary: str, reason: str) -> str:
    safe_summary = (summary or "No summary is available")[:350]
    safe_reason = (reason or "caller requested assistance")[:160]
    message = f"CallFlow AI handoff. Reason: {safe_reason}. Summary: {safe_summary}."
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{_xml(message)}</Say></Response>'
