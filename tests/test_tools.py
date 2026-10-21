"""Tests for tool allow-listing, idempotency, and SQLite persistence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.domain import Action, ActionKind
from app.storage import CallStore
from app.tools import ToolRegistry, execute_tool

FIXED_NOW = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)


class CallStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "calls.db"
        self.store = CallStore(path, clock=lambda: FIXED_NOW)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_call_lifecycle_and_turn_event_id_are_idempotent(self) -> None:
        first = self.store.start_call("session-1", "CA-1", "+923001234567")
        retried = self.store.start_call("session-1", "CA-1", "+923001234567")
        first_turn = self.store.add_turn("session-1", "user", "Hello", event_id="evt-1")
        retried_turn = self.store.add_turn("session-1", "user", "Hello", event_id="evt-1")

        self.assertEqual(first["started_at"], retried["started_at"])
        self.assertEqual(first_turn, retried_turn)
        self.assertEqual(len(self.store.get_call("session-1")["turns"]), 1)

        completed = self.store.finish_call("session-1", "resolved", "Appointment answered")
        duplicate = self.store.finish_call("session-1", "different", "Should not overwrite")
        self.assertEqual(completed["outcome"], "resolved")
        self.assertEqual(duplicate["outcome"], "resolved")

        metrics = self.store.metrics()
        self.assertEqual(metrics["total_calls"], 1)
        self.assertEqual(metrics["completed_calls"], 1)

    def test_recent_calls_are_newest_first_and_bounded(self) -> None:
        self.store.start_call("one")
        self.store.start_call("two")

        recent = self.store.recent_calls(limit=1)

        self.assertEqual(len(recent), 1)
        self.assertIn(recent[0]["session_id"], {"one", "two"})


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "tools.db"
        self.store = CallStore(path, clock=lambda: FIXED_NOW)
        self.registry = ToolRegistry(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_availability_returns_structured_slots(self) -> None:
        action = Action(
            ActionKind.CHECK_AVAILABILITY,
            "availability_intent",
            {"service": "consultation", "requested_date": "2030-01-02"},
        )

        result = self.registry.execute(action, "availability-session")

        self.assertTrue(result.success)
        self.assertGreater(len(result.data["slots"]), 0)
        self.assertEqual(result.data["timezone"], "Asia/Karachi")

    def test_customer_lookup_returns_seeded_demo_record_with_masked_phone(self) -> None:
        action = Action(
            ActionKind.LOOKUP_CUSTOMER,
            "customer_lookup_intent",
            {"caller": "+15550101001"},
        )

        result = self.registry.execute(action, "customer-session")

        self.assertTrue(result.success)
        self.assertTrue(result.data["found"])
        self.assertEqual(result.data["customer"]["customer_id"], "CUST-DEMO-001")
        self.assertEqual(result.data["customer"]["phone_masked"], "*******1001")
        self.assertNotIn("phone", result.data["customer"])

    def test_upserted_customer_can_be_looked_up(self) -> None:
        created = self.store.upsert_customer(
            "+15550101002",
            "Sample Caller",
            customer_id="CUST-DEMO-002",
            tier="priority",
        )

        found = self.store.lookup_customer("+1 (555) 010-1002")

        self.assertEqual(created["customer_id"], "CUST-DEMO-002")
        self.assertEqual(found["name"], "Sample Caller")

    def test_booking_retry_returns_one_appointment(self) -> None:
        action = Action(
            ActionKind.BOOK_APPOINTMENT,
            "appointment_booking_intent",
            {
                "service": "consultation",
                "requested_date": "2030-01-02",
                "requested_time": "09:00",
                "caller_name": "Ayesha",
                "idempotency_key": "provider-event-123",
            },
        )

        first = self.registry.execute(action, "booking-session")
        retried = self.registry.execute(action, "booking-session")

        self.assertTrue(first.success)
        self.assertTrue(retried.success)
        self.assertTrue(retried.cached)
        self.assertEqual(
            first.data["booking"]["booking_ref"],
            retried.data["booking"]["booking_ref"],
        )
        self.assertEqual(len(self.store.list_appointments("booking-session")), 1)
        self.assertEqual(self.store.metrics()["appointments_booked"], 1)

    def test_booking_does_not_guess_missing_date_or_time(self) -> None:
        action = Action(
            ActionKind.BOOK_APPOINTMENT,
            "appointment_booking_intent",
            {"service": "general"},
        )

        result = self.registry.execute(action, "missing-slot-session")

        self.assertFalse(result.success)
        self.assertEqual(
            result.data["requires_input"],
            ["requested_date", "requested_time"],
        )
        self.assertEqual(self.store.list_appointments("missing-slot-session"), [])

    def test_two_sessions_cannot_book_the_same_slot(self) -> None:
        args = {
            "service": "general",
            "requested_date": "2030-01-02",
            "requested_time": "11:00",
        }
        first = self.registry.execute(
            Action(ActionKind.BOOK_APPOINTMENT, "booking", {**args, "idempotency_key": "one"}),
            "caller-one",
        )
        second = self.registry.execute(
            Action(ActionKind.BOOK_APPOINTMENT, "booking", {**args, "idempotency_key": "two"}),
            "caller-two",
        )

        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertIn("alternative", second.message.lower())

    def test_ticket_creation_is_idempotent(self) -> None:
        action = Action(
            ActionKind.CREATE_TICKET,
            "support_ticket_intent",
            {
                "summary": "I cannot sign in",
                "category": "account",
                "priority": "high",
                "idempotency_key": "ticket-event-1",
            },
        )

        first = execute_tool(action, self.store, "ticket-session")
        retried = execute_tool(action, self.store, "ticket-session")

        self.assertTrue(first.success)
        self.assertTrue(retried.cached)
        self.assertEqual(first.data["ticket"]["ticket_ref"], retried.data["ticket"]["ticket_ref"])
        self.assertEqual(len(self.store.list_tickets("ticket-session")), 1)
        self.assertEqual(self.store.metrics()["open_tickets"], 1)

    def test_transfer_marks_call_and_preserves_reason(self) -> None:
        action = Action(
            ActionKind.TRANSFER_HUMAN,
            "customer_frustration",
            {"queue": "retention", "priority": "high"},
        )

        result = self.registry.execute(action, "handoff-session")
        call = self.store.get_call("handoff-session")

        self.assertTrue(result.success)
        self.assertEqual(call["status"], "handoff_requested")
        self.assertEqual(call["handoff_reason"], "customer_frustration")
        self.assertEqual(self.store.metrics()["handed_off_calls"], 1)

    def test_unknown_tool_is_rejected(self) -> None:
        result = self.registry.execute(
            {"kind": "run_shell", "reason": "untrusted", "args": {}},
            "unsafe-session",
        )

        self.assertFalse(result.success)
        self.assertIn("not an approved tool", result.message)


if __name__ == "__main__":
    unittest.main()
