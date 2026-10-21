"""Tests for deterministic routing and escalation policy."""

from __future__ import annotations

import unittest

from app.domain import ActionKind, ConversationState, decide_action


class DecideActionTests(unittest.TestCase):
    def test_high_risk_always_overrides_booking(self) -> None:
        action = decide_action("Book tomorrow, but I have chest pain and can't breathe")

        self.assertEqual(action.kind, ActionKind.TRANSFER_HUMAN)
        self.assertEqual(action.reason, "safety_risk")
        self.assertEqual(action.args["priority"], "urgent")

    def test_explicit_human_request_is_honoured_in_roman_urdu(self) -> None:
        action = decide_action("Mujhe agent se baat karni hai")

        self.assertEqual(action.kind, ActionKind.TRANSFER_HUMAN)
        self.assertEqual(action.reason, "explicit_human_request")

    def test_frustration_signal_triggers_handoff(self) -> None:
        action = decide_action("This is useless, it is still not working")

        self.assertEqual(action.kind, ActionKind.TRANSFER_HUMAN)
        self.assertEqual(action.reason, "customer_frustration")
        self.assertEqual(action.args["priority"], "high")

    def test_accumulated_frustration_triggers_handoff(self) -> None:
        state = ConversationState(frustration_count=2)

        action = decide_action("Can you try once more?", state)

        self.assertEqual(action.kind, ActionKind.TRANSFER_HUMAN)
        self.assertEqual(action.reason, "customer_frustration")

    def test_booking_extracts_iso_date_and_normalised_time(self) -> None:
        state = {"caller": "+923001234567", "caller_name": "Ayesha", "service": "consultation"}

        action = decide_action("Please book appointment on 2030-01-02 at 2 pm", state)

        self.assertEqual(action.kind, ActionKind.BOOK_APPOINTMENT)
        self.assertEqual(action.args["requested_date"], "2030-01-02")
        self.assertEqual(action.args["requested_time"], "14:00")
        self.assertEqual(action.args["service"], "consultation")
        self.assertEqual(action.args["missing"], [])

    def test_booking_collects_missing_fields_instead_of_guessing(self) -> None:
        action = decide_action("Please book an appointment")

        self.assertEqual(action.kind, ActionKind.BOOK_APPOINTMENT)
        self.assertEqual(action.args["missing"], ["requested_date", "requested_time"])
        self.assertIn("date and time", action.reply)

    def test_pending_booking_accepts_follow_up_slot(self) -> None:
        state = ConversationState(pending_intent=ActionKind.BOOK_APPOINTMENT)

        action = decide_action("Tomorrow at 11 am", state)

        self.assertEqual(action.kind, ActionKind.BOOK_APPOINTMENT)
        self.assertEqual(action.args["requested_date"], "tomorrow")
        self.assertEqual(action.args["requested_time"], "11:00")

    def test_availability_route(self) -> None:
        action = decide_action("What available slots do you have tomorrow?")

        self.assertEqual(action.kind, ActionKind.CHECK_AVAILABILITY)
        self.assertEqual(action.args["requested_date"], "tomorrow")

    def test_customer_profile_route_uses_caller_context(self) -> None:
        action = decide_action("Please look up my account details", {"caller": "+15550101001"})

        self.assertEqual(action.kind, ActionKind.LOOKUP_CUSTOMER)
        self.assertEqual(action.args["caller"], "+15550101001")

    def test_plain_my_account_phrase_routes_to_customer_lookup(self) -> None:
        action = decide_action("I want to see my account", {"caller": "+15550101001"})

        self.assertEqual(action.kind, ActionKind.LOOKUP_CUSTOMER)

    def test_support_problem_creates_ticket_action(self) -> None:
        action = decide_action("My login is not working and shows an error message")

        self.assertEqual(action.kind, ActionKind.CREATE_TICKET)
        self.assertEqual(action.args["category"], "support")
        self.assertIn("login", action.args["summary"].lower())

    def test_goodbye_ends_call(self) -> None:
        action = decide_action("That's all, goodbye")

        self.assertEqual(action.kind, ActionKind.END_CALL)

    def test_empty_audio_requests_repeat(self) -> None:
        action = decide_action("   ")

        self.assertEqual(action.kind, ActionKind.RESPOND)
        self.assertEqual(action.reason, "empty_or_inaudible_input")


if __name__ == "__main__":
    unittest.main()
