"""Allow-listed tool execution for deterministic voice-agent actions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .domain import Action, ActionKind
from .storage import CallStore, StorageConflict


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A transport-neutral result that can be spoken or returned as JSON."""

    success: bool
    name: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    cached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, cached: bool = False) -> ToolResult:
        return cls(
            success=bool(value.get("success", False)),
            name=str(value.get("name", "")),
            message=str(value.get("message", "")),
            data=dict(value.get("data", {})),
            cached=cached or bool(value.get("cached", False)),
        )


class ToolRegistry:
    """Execute only known tools and persist mutation results for safe retries."""

    _MUTATING_ACTIONS = frozenset(
        {
            ActionKind.BOOK_APPOINTMENT,
            ActionKind.CREATE_TICKET,
            ActionKind.TRANSFER_HUMAN,
        }
    )

    def __init__(self, store: CallStore) -> None:
        self.store = store
        self._handlers: dict[str, Callable[[str, dict[str, Any], str, str], ToolResult]] = {
            ActionKind.LOOKUP_CUSTOMER: self._lookup_customer,
            ActionKind.CHECK_AVAILABILITY: self._check_availability,
            ActionKind.BOOK_APPOINTMENT: self._book_appointment,
            ActionKind.CREATE_TICKET: self._create_ticket,
            ActionKind.TRANSFER_HUMAN: self._transfer_human,
        }

    def register(
        self,
        action_kind: str,
        handler: Callable[[str, dict[str, Any], str, str], ToolResult],
    ) -> None:
        """Register an intentional local extension; existing tools cannot be replaced."""

        if not action_kind or action_kind in self._handlers:
            raise ValueError("action kind is empty or already registered")
        self._handlers[action_kind] = handler

    @staticmethod
    def _coerce_action(action: Action | Mapping[str, Any]) -> Action:
        if isinstance(action, Action):
            return action
        return Action(
            kind=str(action.get("kind", "")),
            reason=str(action.get("reason", "")),
            args=dict(action.get("args", {})),
            reply=str(action.get("reply", "")),
        )

    @staticmethod
    def _idempotency_key(action: Action) -> str:
        supplied = str(action.args.get("idempotency_key", "")).strip()
        if supplied:
            return supplied[:160]
        canonical = json.dumps(
            {"kind": action.kind, "reason": action.reason, "args": action.args},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"auto-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:40]}"

    def execute(self, action: Action | Mapping[str, Any], session_id: str) -> ToolResult:
        """Execute an action through the allow-list and return a structured result."""

        decision = self._coerce_action(action)
        session_id = str(session_id or "").strip()
        if not session_id:
            return ToolResult(False, decision.kind, "A session_id is required to run a tool.")

        if decision.kind in {ActionKind.RESPOND, ActionKind.END_CALL}:
            return ToolResult(
                True,
                decision.kind,
                decision.reply,
                {"executed": False, "reason": decision.reason},
            )

        handler = self._handlers.get(decision.kind)
        if handler is None:
            return ToolResult(False, decision.kind, "That action is not an approved tool.")

        caller = str(decision.args.get("caller", ""))
        self.store.start_call(session_id, caller=caller)
        key = self._idempotency_key(decision)
        if decision.kind in self._MUTATING_ACTIONS:
            cached = self.store.get_tool_execution(session_id, key)
            if cached is not None:
                return ToolResult.from_dict(cached["response"], cached=True)

        try:
            result = handler(session_id, dict(decision.args), decision.reason, key)
        except (ValueError, KeyError, StorageConflict) as exc:
            result = ToolResult(False, decision.kind, str(exc), {"error": type(exc).__name__})

        if decision.kind in self._MUTATING_ACTIONS:
            stored = self.store.record_tool_execution(
                session_id,
                key,
                decision.kind,
                decision.as_dict(),
                result.as_dict(),
                result.success,
            )
            return ToolResult.from_dict(stored["response"])
        return result

    @staticmethod
    def _mask_phone(phone: str) -> str:
        digits = "".join(character for character in phone if character.isdigit())
        if len(digits) <= 4:
            return "*" * len(digits)
        return f"{'*' * (len(digits) - 4)}{digits[-4:]}"

    def _lookup_customer(
        self,
        session_id: str,
        args: dict[str, Any],
        reason: str,
        key: str,
    ) -> ToolResult:
        del session_id, reason, key
        caller = str(args.get("caller", "")).strip()
        if not caller:
            return ToolResult(
                False,
                ActionKind.LOOKUP_CUSTOMER,
                "I need the caller phone number before I can securely look up a profile.",
                {"found": False, "requires_input": ["caller"]},
            )
        try:
            customer = self.store.lookup_customer(caller)
        except ValueError:
            return ToolResult(
                False,
                ActionKind.LOOKUP_CUSTOMER,
                "That caller number isn't in a valid phone format.",
                {"found": False, "requires_input": ["caller"]},
            )
        if customer is None:
            return ToolResult(
                False,
                ActionKind.LOOKUP_CUSTOMER,
                "I couldn't find a customer profile linked to this caller number.",
                {"found": False},
            )
        safe_customer = {
            "customer_id": customer["customer_id"],
            "name": customer["name"],
            "tier": customer["tier"],
            "status": customer["status"],
            "phone_masked": self._mask_phone(customer["phone"]),
        }
        return ToolResult(
            True,
            ActionKind.LOOKUP_CUSTOMER,
            f"I found the {customer['status']} {customer['tier']} profile for {customer['name']}.",
            {"found": True, "customer": safe_customer},
        )

    def _check_availability(
        self,
        session_id: str,
        args: dict[str, Any],
        reason: str,
        key: str,
    ) -> ToolResult:
        del session_id, reason, key
        service = str(args.get("service", "general") or "general")
        requested_date = args.get("requested_date")
        slots = self.store.list_available_slots(
            service,
            requested_date,
            business_days=3 if requested_date else 5,
        )
        if not slots:
            return ToolResult(
                False,
                ActionKind.CHECK_AVAILABILITY,
                "I couldn't find an open slot in that window. I can check another date.",
                {"slots": [], "service": service, "requires_input": ["requested_date"]},
            )
        spoken = ", ".join(slot["display"] for slot in slots[:3])
        return ToolResult(
            True,
            ActionKind.CHECK_AVAILABILITY,
            f"The first available times are {spoken}. Which one works for you?",
            {"slots": slots[:12], "service": service, "timezone": self.store.timezone_name},
        )

    def _book_appointment(
        self,
        session_id: str,
        args: dict[str, Any],
        reason: str,
        key: str,
    ) -> ToolResult:
        del reason
        missing = [
            label
            for label in ("requested_date", "requested_time")
            if not str(args.get(label, "")).strip()
        ]
        if missing:
            readable = " and ".join(label.removeprefix("requested_") for label in missing)
            return ToolResult(
                False,
                ActionKind.BOOK_APPOINTMENT,
                f"Before I book, please tell me the {readable} you want.",
                {"requires_input": missing},
            )

        requested_day = self.store.resolve_date(str(args["requested_date"]))
        requested_time = str(args["requested_time"])
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", requested_time):
            raise ValueError("requested_time must use HH:MM format")
        service = str(args.get("service", "general") or "general")
        available = self.store.list_available_slots(service, requested_day, business_days=3)
        matching = [
            slot
            for slot in available
            if datetime.fromisoformat(slot["start_at"]).date() == requested_day
            and datetime.fromisoformat(slot["start_at"]).strftime("%H:%M") == requested_time
        ]
        if not matching:
            alternatives = [
                slot
                for slot in available
                if datetime.fromisoformat(slot["start_at"]).date() >= requested_day
            ]
            return ToolResult(
                False,
                ActionKind.BOOK_APPOINTMENT,
                "That exact time isn't available. I can offer one of the alternative slots.",
                {
                    "requires_input": ["slot_confirmation"],
                    "alternatives": alternatives[:3],
                    "timezone": self.store.timezone_name,
                },
            )

        booking = self.store.book_appointment(
            session_id,
            matching[0]["start_at"],
            service=service,
            caller=str(args.get("caller", "")),
            caller_name=str(args.get("caller_name", "")),
            idempotency_key=key,
        )
        display = datetime.fromisoformat(booking["start_at"]).strftime("%A, %d %b at %I:%M %p")
        return ToolResult(
            True,
            ActionKind.BOOK_APPOINTMENT,
            (
                f"Your appointment is confirmed for {display}. "
                f"Your reference is {booking['booking_ref']}."
            ),
            {"booking": booking},
        )

    def _create_ticket(
        self,
        session_id: str,
        args: dict[str, Any],
        reason: str,
        key: str,
    ) -> ToolResult:
        del reason
        summary = str(args.get("summary", "")).strip()
        if not summary:
            return ToolResult(
                False,
                ActionKind.CREATE_TICKET,
                "Please briefly describe the issue before I create a ticket.",
                {"requires_input": ["summary"]},
            )
        ticket = self.store.create_ticket(
            session_id,
            summary,
            category=str(args.get("category", "support")),
            priority=str(args.get("priority", "normal")),
            idempotency_key=key,
        )
        return ToolResult(
            True,
            ActionKind.CREATE_TICKET,
            f"Your support ticket is open. The reference is {ticket['ticket_ref']}.",
            {"ticket": ticket},
        )

    def _transfer_human(
        self,
        session_id: str,
        args: dict[str, Any],
        reason: str,
        key: str,
    ) -> ToolResult:
        del key
        reason = reason or str(args.get("reason", "human_requested"))
        call = self.store.request_handoff(session_id, reason)
        return ToolResult(
            True,
            ActionKind.TRANSFER_HUMAN,
            (
                "I'm connecting you to a human specialist now. "
                "Your conversation context will carry over."
            ),
            {
                "handoff": {
                    "requested": True,
                    "reason": reason,
                    "queue": str(args.get("queue", "support")),
                    "priority": str(args.get("priority", "normal")),
                    "status": call["status"],
                }
            },
        )


def execute_tool(
    action: Action | Mapping[str, Any],
    store: CallStore,
    session_id: str,
) -> ToolResult:
    """Convenience entry point for transports that do not retain a registry."""

    return ToolRegistry(store).execute(action, session_id)


__all__ = ["ToolRegistry", "ToolResult", "execute_tool"]
