"""Deterministic conversation routing for the CallFlow voice agent.

The realtime transport may use an LLM to make speech sound natural, but business
decisions stay here.  Keeping routing deterministic makes safety escalation and
tool execution testable, observable, and independent of a model provider.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


class ActionKind:
    """Stable action names shared by the API, tool registry, and tests."""

    RESPOND = "respond"
    LOOKUP_CUSTOMER = "lookup_customer"
    CHECK_AVAILABILITY = "check_availability"
    BOOK_APPOINTMENT = "book_appointment"
    CREATE_TICKET = "create_ticket"
    TRANSFER_HUMAN = "transfer_human"
    END_CALL = "end_call"


@dataclass(frozen=True, slots=True)
class Action:
    """A routing decision that can be spoken and/or executed as a tool."""

    kind: str
    reason: str
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        return asdict(self)


@dataclass(slots=True)
class ConversationState:
    """Small provider-neutral state bag used across realtime turns."""

    session_id: str = ""
    caller: str = ""
    caller_name: str = ""
    service: str = "general"
    pending_intent: str = ""
    requested_date: str = ""
    requested_time: str = ""
    frustration_count: int = 0
    turn_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


_HIGH_RISK_PHRASES = (
    "can't breathe",
    "cannot breathe",
    "difficulty breathing",
    "chest pain",
    "severe bleeding",
    "unconscious",
    "overdose",
    "suicidal",
    "suicide",
    "kill myself",
    "hurt myself",
    "immediate danger",
    "medical emergency",
    "life threatening",
    "account hacked",
    "identity stolen",
    "fraud happening",
    "jaan ko khatra",
    "saans nahi aa",
    "khoon nahi ruk",
)

_HUMAN_REQUEST_PHRASES = (
    "human agent",
    "real person",
    "live agent",
    "representative",
    "supervisor",
    "operator",
    "speak to a human",
    "talk to a human",
    "transfer me",
    "connect me",
    "insaan se baat",
    "bande se baat",
    "agent se baat",
    "team se baat",
    "human se baat",
)

_FRUSTRATION_PHRASES = (
    "frustrated",
    "very angry",
    "this is useless",
    "not helping",
    "waste of time",
    "third time",
    "again and again",
    "terrible service",
    "ridiculous",
    "fed up",
    "bekar service",
    "fazool",
    "tang aa gaya",
    "tang aa gayi",
    "samajh nahi aa rahi tumhe",
)

_BOOKING_PHRASES = (
    "book appointment",
    "book an appointment",
    "book me",
    "reserve appointment",
    "confirm appointment",
    "schedule appointment",
    "schedule me",
    "appointment book",
    "booking kar",
    "appointment bana",
)

_AVAILABILITY_PHRASES = (
    "availability",
    "available slot",
    "available time",
    "open slot",
    "next slot",
    "appointment time",
    "what times",
    "which times",
    "koi slot",
    "khali slot",
    "time available",
)

_CUSTOMER_LOOKUP_PHRASES = (
    "my account",
    "my account details",
    "my account information",
    "my profile",
    "customer record",
    "customer profile",
    "view profile",
    "look up my account",
    "lookup my account",
    "check my account",
    "find my account",
    "who am i",
    "mera account",
    "meri profile",
    "account check kar",
)

_TICKET_PHRASES = (
    "create ticket",
    "open ticket",
    "raise ticket",
    "open a case",
    "create a case",
    "report issue",
    "lodge complaint",
    "complaint register",
    "ticket bana",
    "complaint darj",
)

_SUPPORT_PHRASES = (
    "not working",
    "technical issue",
    "support issue",
    "login problem",
    "payment problem",
    "billing problem",
    "error message",
    "charged twice",
    "refund problem",
    "help with my account",
    "masla ho raha",
    "kaam nahi kar",
)

_END_PHRASES = (
    "goodbye",
    "bye bye",
    "that's all",
    "that is all",
    "no more help",
    "call end",
    "allah hafiz",
    "khuda hafiz",
)


def _normalise(text: str) -> str:
    lowered = text.casefold().replace("’", "'")
    return " ".join(lowered.split())


def _contains(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _state_value(
    state: ConversationState | Mapping[str, Any] | None, key: str, default: Any = ""
) -> Any:
    if state is None:
        return default
    if isinstance(state, Mapping):
        return state.get(key, default)
    return getattr(state, key, default)


def _valid_iso_date(candidate: str) -> bool:
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _extract_date(text: str, state: ConversationState | Mapping[str, Any] | None) -> str:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if match and _valid_iso_date(match.group(1)):
        return match.group(1)

    if "day after tomorrow" in text or "parson" in text:
        return "day_after_tomorrow"
    if "tomorrow" in text or "kal" in text:
        return "tomorrow"
    if "today" in text or "aaj" in text:
        return "today"

    stored = str(_state_value(state, "requested_date", "") or "")
    return stored


def _extract_time(text: str, state: ConversationState | Mapping[str, Any] | None) -> str:
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(am|pm)?\b", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        meridiem = match.group(3)
        if meridiem:
            if hour > 12:
                return ""
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
        return f"{hour:02d}:{minute:02d}"

    match = re.search(r"\b(1[0-2]|0?[1-9])\s*(am|pm)\b", text)
    if match:
        hour = int(match.group(1))
        if match.group(2) == "pm" and hour != 12:
            hour += 12
        elif match.group(2) == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:00"

    stored = str(_state_value(state, "requested_time", "") or "")
    return stored


def _shared_args(text: str, state: ConversationState | Mapping[str, Any] | None) -> dict[str, Any]:
    args: dict[str, Any] = {}
    requested_date = _extract_date(text, state)
    requested_time = _extract_time(text, state)
    service = str(_state_value(state, "service", "general") or "general")
    caller = str(_state_value(state, "caller", "") or "")
    caller_name = str(_state_value(state, "caller_name", "") or "")

    if requested_date:
        args["requested_date"] = requested_date
    if requested_time:
        args["requested_time"] = requested_time
    args["service"] = service
    if caller:
        args["caller"] = caller
    if caller_name:
        args["caller_name"] = caller_name
    return args


def decide_action(
    text: str,
    state: ConversationState | Mapping[str, Any] | None = None,
) -> Action:
    """Choose the next action using transparent, deterministic rules.

    Safety and human-choice rules deliberately run before business intents.  The
    function has no side effects; callers decide when to update state or execute
    a tool.
    """

    raw_text = str(text or "").strip()
    normalised = _normalise(raw_text)

    if not normalised:
        return Action(
            kind=ActionKind.RESPOND,
            reason="empty_or_inaudible_input",
            reply="I didn't catch that. Could you please say it once more?",
        )

    if _contains(normalised, _HIGH_RISK_PHRASES):
        return Action(
            kind=ActionKind.TRANSFER_HUMAN,
            reason="safety_risk",
            args={"priority": "urgent", "queue": "safety", "context": raw_text[:500]},
            reply=(
                "This may need urgent human help, so I'm escalating now. "
                "If anyone is in immediate danger, please contact local emergency services."
            ),
        )

    if _contains(normalised, _HUMAN_REQUEST_PHRASES):
        return Action(
            kind=ActionKind.TRANSFER_HUMAN,
            reason="explicit_human_request",
            args={"priority": "normal", "queue": "support", "context": raw_text[:500]},
            reply=(
                "Of course. I'm connecting you to a human specialist and "
                "passing along this context."
            ),
        )

    frustration_count = int(_state_value(state, "frustration_count", 0) or 0)
    if _contains(normalised, _FRUSTRATION_PHRASES) or frustration_count >= 2:
        return Action(
            kind=ActionKind.TRANSFER_HUMAN,
            reason="customer_frustration",
            args={"priority": "high", "queue": "retention", "context": raw_text[:500]},
            reply=(
                "I'm sorry this has been frustrating. I'll bring in a human specialist now "
                "so you don't have to repeat yourself."
            ),
        )

    if _contains(normalised, _END_PHRASES):
        return Action(
            kind=ActionKind.END_CALL,
            reason="caller_finished",
            reply="Thank you for calling. Take care and goodbye.",
        )

    args = _shared_args(normalised, state)
    pending_intent = str(_state_value(state, "pending_intent", "") or "").casefold()
    has_slot_details = "requested_date" in args or "requested_time" in args

    if _contains(normalised, _BOOKING_PHRASES) or (
        pending_intent == ActionKind.BOOK_APPOINTMENT and has_slot_details
    ):
        missing = [field for field in ("requested_date", "requested_time") if field not in args]
        if missing:
            label = " and ".join(item.removeprefix("requested_") for item in missing)
            reply = f"I can book that. What {label} would you prefer?"
        else:
            reply = "I'll verify that slot and book it if it is still available."
        args["missing"] = missing
        return Action(
            kind=ActionKind.BOOK_APPOINTMENT,
            reason="appointment_booking_intent",
            args=args,
            reply=reply,
        )

    if _contains(normalised, _AVAILABILITY_PHRASES):
        return Action(
            kind=ActionKind.CHECK_AVAILABILITY,
            reason="availability_intent",
            args=args,
            reply="I'll check the next available appointment times for you.",
        )

    if _contains(normalised, _CUSTOMER_LOOKUP_PHRASES):
        return Action(
            kind=ActionKind.LOOKUP_CUSTOMER,
            reason="customer_lookup_intent",
            args=args,
            reply="I'll securely look up the customer profile linked to this caller number.",
        )

    if _contains(normalised, _TICKET_PHRASES) or _contains(normalised, _SUPPORT_PHRASES):
        args.update(
            {
                "summary": raw_text[:1000],
                "category": "support",
                "priority": "normal",
            }
        )
        return Action(
            kind=ActionKind.CREATE_TICKET,
            reason="support_ticket_intent",
            args=args,
            reply="I'll create a support ticket with what you've told me.",
        )

    if normalised in {"hi", "hello", "hey", "salam", "assalamualaikum", "assalam o alaikum"}:
        return Action(
            kind=ActionKind.RESPOND,
            reason="greeting",
            reply=(
                "Hello! I can check appointment availability, make a booking, help with support, "
                "or connect you to a person. How can I help?"
            ),
        )

    return Action(
        kind=ActionKind.RESPOND,
        reason="clarification_needed",
        reply=(
            "I can help with appointments or support. Tell me what you need, "
            "or say 'human agent' at any time."
        ),
    )


__all__ = ["Action", "ActionKind", "ConversationState", "decide_action"]
