"""SQLite persistence for calls, turns, tools, appointments, and tickets."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class StorageConflict(RuntimeError):
    """Raised when a unique resource, such as an appointment slot, is taken."""


class CallStore:
    """Small SQLite repository designed for webhook and websocket retries.

    Each operation uses a short transaction and a fresh connection, which keeps
    the class safe to share between async request handlers via a thread pool.
    """

    def __init__(
        self,
        db_path: str | Path = "callflow.db",
        *,
        timezone_name: str = "Asia/Karachi",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.timezone_name = timezone_name
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            self._timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self._timezone = timezone.utc
            self.timezone_name = "UTC"

        self._uri = False
        self._anchor: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self.db_path = f"file:callflow-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._anchor = self._new_connection()
        else:
            Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def close(self) -> None:
        """Close the keepalive connection used for an in-memory database."""

        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not self._uri:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._new_connection()

    def _initialise(self) -> None:
        connection = self._anchor or self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    session_id TEXT PRIMARY KEY,
                    call_sid TEXT NOT NULL DEFAULT '',
                    caller TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    outcome TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    handoff_reason TEXT NOT NULL DEFAULT ''
                );

                CREATE UNIQUE INDEX IF NOT EXISTS calls_call_sid_unique
                    ON calls(call_sid) WHERE call_sid <> '';
                CREATE INDEX IF NOT EXISTS calls_started_at_idx ON calls(started_at DESC);

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES calls(session_id) ON DELETE CASCADE,
                    UNIQUE(session_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS turns_session_idx ON turns(session_id, id);

                CREATE TABLE IF NOT EXISTS tool_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES calls(session_id) ON DELETE CASCADE,
                    UNIQUE(session_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS customers (
                    customer_id TEXT PRIMARY KEY,
                    phone TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'standard',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    booking_ref TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    caller TEXT NOT NULL DEFAULT '',
                    caller_name TEXT NOT NULL DEFAULT '',
                    service TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'booked',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES calls(session_id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS appointments_live_slot_unique
                    ON appointments(service, start_at) WHERE status = 'booked';
                CREATE UNIQUE INDEX IF NOT EXISTS appointments_idempotency_unique
                    ON appointments(session_id, idempotency_key) WHERE idempotency_key <> '';

                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_ref TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES calls(session_id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS tickets_idempotency_unique
                    ON tickets(session_id, idempotency_key) WHERE idempotency_key <> '';

                INSERT OR IGNORE INTO customers(
                    customer_id, phone, name, tier, status, created_at, updated_at
                ) VALUES (
                    'CUST-DEMO-001', '+15550101001', 'Demo Customer', 'standard', 'active',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                );
                """
            )
        finally:
            if connection is not self._anchor:
                connection.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def _timestamp(self) -> str:
        return self._now().astimezone(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _text(value: Any, field: str, *, maximum: int, required: bool = False) -> str:
        cleaned = " ".join(str(value or "").replace("\x00", " ").split())
        if required and not cleaned:
            raise ValueError(f"{field} is required")
        if len(cleaned) > maximum:
            raise ValueError(f"{field} must be at most {maximum} characters")
        return cleaned

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def start_call(self, session_id: str, call_sid: str = "", caller: str = "") -> dict[str, Any]:
        """Create a call once; webhook retries return the original call."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        call_sid = self._text(call_sid, "call_sid", maximum=160)
        caller = self._text(caller, "caller", maximum=80)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO calls(session_id, call_sid, caller, started_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING
                    """,
                    (session_id, call_sid, caller, self._timestamp()),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if call_sid:
                    existing = connection.execute(
                        "SELECT session_id FROM calls WHERE call_sid = ?", (call_sid,)
                    ).fetchone()
                    if existing is not None:
                        found = self.get_call(str(existing["session_id"]))
                        if found is not None:
                            return found
                raise StorageConflict("call_sid already belongs to another session") from exc
        finally:
            connection.close()
        call = self.get_call(session_id)
        if call is None:  # Defensive: the insert and read should be atomic from the caller's view.
            raise RuntimeError("call could not be loaded after creation")
        return call

    def add_turn(
        self,
        session_id: str,
        role: str,
        text: str,
        *,
        event_id: str | None = None,
    ) -> int:
        """Append a transcript turn, optionally deduplicated by provider event ID."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        role = self._text(role, "role", maximum=32, required=True).casefold()
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("role must be system, user, assistant, or tool")
        text = self._text(text, "text", maximum=12000, required=True)
        event_id = self._text(event_id or uuid.uuid4().hex, "event_id", maximum=160, required=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO turns(session_id, event_id, role, text, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, event_id) DO NOTHING
                    """,
                    (session_id, event_id, role, text, self._timestamp()),
                )
                if cursor.rowcount:
                    turn_id = int(cursor.lastrowid)
                else:
                    row = connection.execute(
                        "SELECT id FROM turns WHERE session_id = ? AND event_id = ?",
                        (session_id, event_id),
                    ).fetchone()
                    turn_id = int(row["id"])
                connection.commit()
                return turn_id
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise KeyError(f"unknown call session: {session_id}") from exc
        finally:
            connection.close()

    def finish_call(
        self,
        session_id: str,
        outcome: str,
        summary: str,
        handoff_reason: str = "",
    ) -> dict[str, Any]:
        """Finish a call once; later duplicate callbacks do not rewrite its outcome."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        outcome = self._text(outcome, "outcome", maximum=80, required=True)
        summary = self._text(summary, "summary", maximum=4000)
        handoff_reason = self._text(handoff_reason, "handoff_reason", maximum=160)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE calls
                   SET status = 'completed', ended_at = ?, outcome = ?, summary = ?,
                       handoff_reason = CASE WHEN ? <> '' THEN ? ELSE handoff_reason END
                 WHERE session_id = ? AND status <> 'completed'
                """,
                (
                    self._timestamp(),
                    outcome,
                    summary,
                    handoff_reason,
                    handoff_reason,
                    session_id,
                ),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM calls WHERE session_id = ?", (session_id,)
                ).fetchone()
                if exists is None:
                    connection.rollback()
                    raise KeyError(f"unknown call session: {session_id}")
            connection.commit()
        finally:
            connection.close()
        call = self.get_call(session_id)
        if call is None:
            raise RuntimeError("call disappeared after completion")
        return call

    def request_handoff(self, session_id: str, reason: str) -> dict[str, Any]:
        """Mark a live call for human takeover without ending the call."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        reason = self._text(reason, "reason", maximum=160, required=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE calls
                   SET status = CASE
                       WHEN status = 'active' THEN 'handoff_requested'
                       ELSE status
                   END,
                       handoff_reason = CASE WHEN handoff_reason = '' THEN ? ELSE handoff_reason END
                 WHERE session_id = ?
                """,
                (reason, session_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise KeyError(f"unknown call session: {session_id}")
            connection.commit()
        finally:
            connection.close()
        call = self.get_call(session_id)
        if call is None:
            raise RuntimeError("call disappeared after handoff")
        return call

    def update_call_summary(self, session_id: str, summary: str) -> dict[str, Any]:
        """Persist handoff context without prematurely completing the call."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        summary = self._text(summary, "summary", maximum=4000)
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE calls SET summary = ? WHERE session_id = ?",
                (summary, session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown call session: {session_id}")
        finally:
            connection.close()
        call = self.get_call(session_id)
        if call is None:
            raise RuntimeError("call disappeared after summary update")
        return call

    def get_call(self, session_id: str) -> dict[str, Any] | None:
        """Return one call with its ordered transcript."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        connection = self._connect()
        try:
            call_row = connection.execute(
                "SELECT * FROM calls WHERE session_id = ?", (session_id,)
            ).fetchone()
            if call_row is None:
                return None
            call = dict(call_row)
            call["turns"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, event_id, role, text, created_at
                      FROM turns WHERE session_id = ? ORDER BY id
                    """,
                    (session_id,),
                ).fetchall()
            ]
            return call
        finally:
            connection.close()

    def recent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest call summaries without loading full transcripts."""

        limit = max(1, min(int(limit), 200))
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT c.*, COUNT(t.id) AS turn_count
                  FROM calls AS c
                  LEFT JOIN turns AS t ON t.session_id = c.session_id
                 GROUP BY c.session_id
                 ORDER BY c.started_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def metrics(self) -> dict[str, Any]:
        """Return dashboard-ready aggregate call and workflow metrics."""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total_calls,
                       SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_calls,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_calls,
                       SUM(CASE WHEN handoff_reason <> '' THEN 1 ELSE 0 END) AS handed_off_calls,
                       AVG(CASE WHEN ended_at IS NOT NULL
                           THEN (julianday(ended_at) - julianday(started_at)) * 86400 END)
                           AS average_duration_seconds
                  FROM calls
                """
            ).fetchone()
            appointments = connection.execute(
                "SELECT COUNT(*) AS count FROM appointments WHERE status = 'booked'"
            ).fetchone()["count"]
            tickets = connection.execute(
                "SELECT COUNT(*) AS count FROM tickets WHERE status = 'open'"
            ).fetchone()["count"]
            result = dict(row)
            for key in ("total_calls", "active_calls", "completed_calls", "handed_off_calls"):
                result[key] = int(result[key] or 0)
            result["average_duration_seconds"] = round(
                float(result["average_duration_seconds"] or 0), 2
            )
            result["appointments_booked"] = int(appointments)
            result["open_tickets"] = int(tickets)
            return result
        finally:
            connection.close()

    @staticmethod
    def _phone(value: str) -> str:
        """Normalise an E.164-style phone number without accepting free-form SQL keys."""

        compact = re.sub(r"[\s().-]", "", str(value or ""))
        if compact.startswith("00"):
            compact = "+" + compact[2:]
        if not re.fullmatch(r"\+?[0-9]{7,15}", compact):
            raise ValueError("caller phone must contain 7 to 15 digits")
        return compact

    def upsert_customer(
        self,
        phone: str,
        name: str,
        *,
        customer_id: str = "",
        tier: str = "standard",
        status: str = "active",
    ) -> dict[str, Any]:
        """Create or update a consented customer record for demo/CRM sync flows."""

        phone = self._phone(phone)
        name = self._text(name, "name", maximum=160, required=True)
        customer_id = self._text(
            customer_id or f"CUST-{uuid.uuid4().hex[:8].upper()}",
            "customer_id",
            maximum=80,
            required=True,
        )
        tier = self._text(tier or "standard", "tier", maximum=40, required=True).casefold()
        status = self._text(status or "active", "status", maximum=40, required=True).casefold()
        now = self._timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO customers(
                    customer_id, phone, name, tier, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    name = excluded.name,
                    tier = excluded.tier,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (customer_id, phone, name, tier, status, now, now),
            )
            row = connection.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
            connection.commit()
            return dict(row)
        finally:
            connection.close()

    def lookup_customer(self, phone: str) -> dict[str, Any] | None:
        """Look up a customer by the normalised caller phone number."""

        phone = self._phone(phone)
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def resolve_date(self, value: str | date | None) -> date:
        """Resolve ISO and conversational relative dates in the store timezone."""

        today = self._now().astimezone(self._timezone).date()
        if value is None or value == "" or value == "today":
            return today
        if value == "tomorrow":
            return today + timedelta(days=1)
        if value == "day_after_tomorrow":
            return today + timedelta(days=2)
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("requested_date must be an ISO date, today, or tomorrow") from exc

    def list_available_slots(
        self,
        service: str = "general",
        from_date: str | date | None = None,
        *,
        business_days: int = 5,
    ) -> list[dict[str, str]]:
        """Generate open business-hour slots and subtract existing bookings."""

        service = self._text(service or "general", "service", maximum=120, required=True).casefold()
        business_days = max(1, min(int(business_days), 20))
        current = self.resolve_date(from_date)
        dates: list[date] = []
        while len(dates) < business_days:
            if current.weekday() < 5:
                dates.append(current)
            current += timedelta(days=1)

        candidates = [
            datetime.combine(day, time(hour=hour), tzinfo=self._timezone)
            for day in dates
            for hour in (9, 11, 14, 16)
        ]
        canonical = [candidate.isoformat(timespec="minutes") for candidate in candidates]
        connection = self._connect()
        try:
            placeholders = ",".join("?" for _ in canonical)
            rows = connection.execute(
                f"""
                SELECT start_at FROM appointments
                 WHERE service = ? AND status = 'booked' AND start_at IN ({placeholders})
                """,
                (service, *canonical),
            ).fetchall()
            booked = {str(row["start_at"]) for row in rows}
        finally:
            connection.close()
        return [
            {
                "start_at": candidate.isoformat(timespec="minutes"),
                "display": candidate.strftime("%A, %d %b at %I:%M %p"),
                "timezone": self.timezone_name,
            }
            for candidate in candidates
            if candidate.isoformat(timespec="minutes") not in booked
            and candidate > self._now().astimezone(self._timezone)
        ]

    def _parse_start(self, start_at: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(start_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("start_at must be an ISO date-time") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone)
        return parsed.astimezone(self._timezone)

    def book_appointment(
        self,
        session_id: str,
        start_at: str,
        *,
        service: str = "general",
        caller: str = "",
        caller_name: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Reserve one slot atomically and return the same booking on retry."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        service = self._text(service or "general", "service", maximum=120, required=True).casefold()
        caller = self._text(caller, "caller", maximum=80)
        caller_name = self._text(caller_name, "caller_name", maximum=160)
        idempotency_key = self._text(idempotency_key, "idempotency_key", maximum=160)
        start = self._parse_start(start_at)
        if start <= self._now().astimezone(self._timezone):
            raise ValueError("appointment must be in the future")
        if start.weekday() >= 5 or (start.hour, start.minute) not in {
            (9, 0),
            (11, 0),
            (14, 0),
            (16, 0),
        }:
            raise ValueError("appointment must use an offered business-hour slot")
        canonical = start.isoformat(timespec="minutes")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = connection.execute(
                    """
                    SELECT * FROM appointments
                     WHERE session_id = ? AND idempotency_key = ?
                    """,
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return dict(existing)
            booking_ref = f"APT-{uuid.uuid4().hex[:8].upper()}"
            try:
                connection.execute(
                    """
                    INSERT INTO appointments(
                        booking_ref, session_id, caller, caller_name, service, start_at,
                        timezone, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        booking_ref,
                        session_id,
                        caller,
                        caller_name,
                        service,
                        canonical,
                        self.timezone_name,
                        idempotency_key,
                        self._timestamp(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "appointments.service, appointments.start_at" in str(exc):
                    raise StorageConflict("that appointment slot was just taken") from exc
                raise
            row = connection.execute(
                "SELECT * FROM appointments WHERE booking_ref = ?", (booking_ref,)
            ).fetchone()
            connection.commit()
            return dict(row)
        finally:
            connection.close()

    def list_appointments(self, session_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if session_id:
                rows = connection.execute(
                    "SELECT * FROM appointments WHERE session_id = ? ORDER BY start_at",
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM appointments ORDER BY start_at").fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def create_ticket(
        self,
        session_id: str,
        summary: str,
        *,
        category: str = "support",
        priority: str = "normal",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Create one support ticket and deduplicate webhook retries."""

        session_id = self._text(session_id, "session_id", maximum=160, required=True)
        summary = self._text(summary, "summary", maximum=4000, required=True)
        category = self._text(
            category or "support", "category", maximum=120, required=True
        ).casefold()
        priority = self._text(
            priority or "normal", "priority", maximum=32, required=True
        ).casefold()
        if priority not in {"low", "normal", "high", "urgent"}:
            raise ValueError("priority must be low, normal, high, or urgent")
        idempotency_key = self._text(idempotency_key, "idempotency_key", maximum=160)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM tickets WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return dict(existing)
            ticket_ref = f"TKT-{uuid.uuid4().hex[:8].upper()}"
            connection.execute(
                """
                INSERT INTO tickets(
                    ticket_ref, session_id, category, summary, priority,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_ref,
                    session_id,
                    category,
                    summary,
                    priority,
                    idempotency_key,
                    self._timestamp(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_ref = ?", (ticket_ref,)
            ).fetchone()
            connection.commit()
            return dict(row)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise StorageConflict("ticket could not be created") from exc
        finally:
            connection.close()

    def list_tickets(self, session_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if session_id:
                rows = connection.execute(
                    "SELECT * FROM tickets WHERE session_id = ? ORDER BY id DESC", (session_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_tool_execution(self, session_id: str, idempotency_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM tool_executions
                 WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["request"] = json.loads(result.pop("request_json"))
            result["response"] = json.loads(result.pop("response_json"))
            result["success"] = bool(result["success"])
            return result
        finally:
            connection.close()

    def record_tool_execution(
        self,
        session_id: str,
        idempotency_key: str,
        tool_name: str,
        request: dict[str, Any],
        response: dict[str, Any],
        success: bool,
    ) -> dict[str, Any]:
        """Persist a tool result exactly once and return the winning record."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tool_executions(
                    session_id, idempotency_key, tool_name, request_json,
                    response_json, success, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, idempotency_key) DO NOTHING
                """,
                (
                    session_id,
                    idempotency_key,
                    tool_name,
                    json.dumps(request, sort_keys=True, default=str),
                    json.dumps(response, sort_keys=True, default=str),
                    int(success),
                    self._timestamp(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        stored = self.get_tool_execution(session_id, idempotency_key)
        if stored is None:
            raise RuntimeError("tool execution was not persisted")
        return stored


__all__ = ["CallStore", "StorageConflict"]
