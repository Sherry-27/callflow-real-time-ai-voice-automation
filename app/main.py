"""FastAPI transport for Twilio ConversationRelay and the local simulator."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .domain import Action, ActionKind, ConversationState, decide_action
from .groq_stream import GroqStreamClient
from .post_call import send_post_call
from .security import valid_api_key, valid_http_request, valid_websocket
from .session import LiveSession
from .storage import CallStore
from .tools import ToolRegistry, ToolResult
from .twiml import (
    agent_whisper_twiml,
    call_result_twiml,
    conversation_relay_twiml,
    warm_handoff_twiml,
)

app = FastAPI(
    title="CallFlow AI Voice Agent",
    version="1.0.0",
    description="Realtime voice agent with deterministic tools and warm human handoff.",
)
logger = logging.getLogger("callflow")
store = CallStore(settings.database_path)
tool_registry = ToolRegistry(store)
groq = GroqStreamClient(settings.groq_api_key, settings.groq_model)
live_sessions: dict[str, LiveSession] = {}
background_tasks: set[asyncio.Task[bool]] = set()
post_call_scheduled: set[str] = set()


class SimulateRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=160)
    caller: str = Field(default="web-demo", max_length=80)
    text: str = Field(min_length=1, max_length=2000)


def _require_api_key(header_value: str | None, query_value: str | None) -> None:
    if not valid_api_key(header_value or query_value, settings):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _conversation_state(session: LiveSession) -> ConversationState:
    return ConversationState(
        session_id=session.session_id,
        caller=session.caller,
        service=session.pending_fields.get("service", "general"),
        pending_intent=session.pending_fields.get("pending_intent", ""),
        requested_date=session.pending_fields.get("requested_date", ""),
        requested_time=session.pending_fields.get("requested_time", ""),
        frustration_count=session.frustration_score,
        turn_count=sum(item["role"] == "user" for item in session.messages),
    )


def _update_pending(session: LiveSession, action: Action, result: ToolResult | None) -> None:
    for field in ("requested_date", "requested_time", "service"):
        value = action.args.get(field)
        if value:
            session.pending_fields[field] = str(value)
    if action.kind == ActionKind.BOOK_APPOINTMENT and (result is None or not result.success):
        session.pending_fields["pending_intent"] = ActionKind.BOOK_APPOINTMENT
    elif result and result.success:
        session.pending_fields.pop("pending_intent", None)
    if action.reason == "customer_frustration":
        session.frustration_score += 1


def _approved_reply(action: Action, result: ToolResult | None) -> str:
    if result and result.message:
        return result.message
    return action.reply or "I understand. How else may I help?"


async def _reply_tokens(
    session: LiveSession,
    approved_reply: str,
    *,
    naturalize: bool = True,
) -> AsyncIterator[str]:
    if not settings.groq_api_key or not naturalize:
        for chunk in GroqStreamClient._voice_chunks(approved_reply):
            yield chunk
        return
    context = (
        "The business rules approved this exact response. Convey it naturally and briefly, "
        f"without adding facts: {approved_reply}"
    )
    async for token in groq.tokens(session.messages, context, fallback_text=approved_reply):
        yield token


async def _persist_turn(session_id: str, role: str, text: str, event_id: str | None = None) -> None:
    await asyncio.to_thread(store.add_turn, session_id, role, text, event_id=event_id)


async def _prepare_turn(session: LiveSession, text: str) -> tuple[Action, ToolResult | None, str]:
    session.add("user", text)
    await _persist_turn(session.session_id, "user", text)
    action = decide_action(text, _conversation_state(session))
    session.intent = action.kind
    result: ToolResult | None = None
    if action.kind not in {ActionKind.RESPOND, ActionKind.END_CALL}:
        result = await asyncio.to_thread(tool_registry.execute, action, session.session_id)
    _update_pending(session, action, result)
    return action, result, _approved_reply(action, result)


async def _record_answer(session: LiveSession, text: str) -> None:
    session.add("assistant", text)
    await _persist_turn(session.session_id, "assistant", text)


def _schedule_post_call(call: dict) -> None:
    """Deliver a minimal event without delaying a caller-facing response."""

    session_id = str(call.get("session_id", ""))
    if not settings.n8n_post_call_webhook or not session_id or session_id in post_call_scheduled:
        return
    post_call_scheduled.add(session_id)
    payload = {
        "session_id": session_id,
        "call_sid": call.get("call_sid", ""),
        "caller": call.get("caller", ""),
        "status": call.get("status", ""),
        "outcome": call.get("outcome", ""),
        "handoff_reason": call.get("handoff_reason", ""),
        "summary": call.get("summary", ""),
        "started_at": call.get("started_at"),
        "ended_at": call.get("ended_at"),
        "turn_count": len(call.get("turns", [])),
    }
    task = asyncio.create_task(send_post_call(settings.n8n_post_call_webhook, payload))
    background_tasks.add(task)

    def consume_result(done: asyncio.Task[bool]) -> None:
        background_tasks.discard(done)
        try:
            done.result()
        except Exception:  # pragma: no cover - defensive logging around best-effort delivery
            logger.exception("Post-call automation failed unexpectedly")

    task.add_done_callback(consume_result)


async def _finalize(session: LiveSession, outcome: str, reason: str = "") -> dict:
    call = await asyncio.to_thread(
        store.finish_call,
        session.session_id,
        outcome,
        session.short_summary(),
        reason,
    )
    _schedule_post_call(call)
    return call


async def _save_callback_request(session_id: str, reason: str, transfer_status: str) -> bool:
    """Persist an idempotent callback ticket before promising one to the caller."""

    if not session_id:
        return False
    try:
        call = await asyncio.to_thread(store.get_call, session_id)
        if call is None:
            return False
        await asyncio.to_thread(
            store.create_ticket,
            session_id,
            f"Human callback requested. Reason: {reason}. Transfer status: {transfer_status}.",
            category="callback",
            priority="high" if reason == "safety_risk" else "normal",
            idempotency_key=f"callback:{reason}:{transfer_status}"[:160],
        )
        summary = call.get("summary") or f"Caller requested human assistance: {reason}."
        completed = await asyncio.to_thread(
            store.finish_call,
            session_id,
            "callback_requested",
            summary,
            reason,
        )
        _schedule_post_call(completed)
        return True
    except Exception:  # pragma: no cover - protects the telephony fallback path
        logger.exception("Could not persist callback request for %s", session_id)
        return False


async def _settle_response(task: asyncio.Task | None, *, cancel: bool) -> None:
    """Cancel and retrieve a response task so turns cannot race silently."""

    if task is None:
        return
    if cancel and not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Voice response task failed")


async def _run_simulated_turn(session: LiveSession, text: str) -> dict:
    action, result, approved = await _prepare_turn(session, text)
    tokens = [token async for token in _reply_tokens(session, approved)]
    reply = "".join(tokens).strip() or approved
    await _record_answer(session, reply)
    handoff = action.kind == ActionKind.TRANSFER_HUMAN
    reason = action.reason if handoff else ""
    if handoff:
        await _finalize(session, "handed_off", reason)
    elif action.kind == ActionKind.END_CALL:
        await _finalize(session, "self_service_complete")
    return {
        "session_id": session.session_id,
        "reply": reply,
        "action": action.kind,
        "handoff": handoff,
        "handoff_reason": reason,
        "tool_result": result.as_dict() if result else None,
    }


async def _send_voice_turn(websocket: WebSocket, session: LiveSession, text: str) -> None:
    # Tool work is shielded: cancelling speech generation must not leave an
    # appointment/ticket thread running without its result being observed.
    preparation = asyncio.create_task(_prepare_turn(session, text))
    try:
        action, result, approved = await asyncio.shield(preparation)
    except asyncio.CancelledError:
        action, _result, _approved = await preparation
        if action.kind == ActionKind.TRANSFER_HUMAN:
            session.handoff_in_progress = True
            session.handoff_reason = action.reason
            await asyncio.to_thread(
                store.update_call_summary, session.session_id, session.short_summary()
            )
            await websocket.send_json(
                {
                    "type": "end",
                    "handoffData": json.dumps(
                        {
                            "reasonCode": "live-agent-handoff",
                            "sessionId": session.session_id,
                            "reason": action.reason,
                        }
                    ),
                }
            )
        raise

    locked_handoff = action.kind == ActionKind.TRANSFER_HUMAN
    if locked_handoff:
        session.handoff_in_progress = True
        session.handoff_reason = action.reason
    pending: str | None = None
    spoken: list[str] = []
    async for token in _reply_tokens(session, approved, naturalize=not locked_handoff):
        if pending is not None:
            await websocket.send_json(
                {
                    "type": "text",
                    "token": pending,
                    "last": False,
                    "lang": "multi",
                    "interruptible": not locked_handoff,
                    "preemptible": not locked_handoff,
                }
            )
            spoken.append(pending)
        pending = token
    final_token = pending or approved
    await websocket.send_json(
        {
            "type": "text",
            "token": final_token,
            "last": True,
            "lang": "multi",
            "interruptible": not locked_handoff,
            "preemptible": not locked_handoff,
        }
    )
    spoken.append(final_token)
    await _record_answer(session, "".join(spoken).strip())

    if action.kind == ActionKind.TRANSFER_HUMAN:
        await asyncio.to_thread(
            store.update_call_summary, session.session_id, session.short_summary()
        )
        await websocket.send_json(
            {
                "type": "end",
                "handoffData": json.dumps(
                    {
                        "reasonCode": "live-agent-handoff",
                        "sessionId": session.session_id,
                        "reason": action.reason,
                    }
                ),
            }
        )
    elif action.kind == ActionKind.END_CALL:
        await _finalize(session, "self_service_complete")
        await websocket.send_json(
            {
                "type": "end",
                "handoffData": json.dumps(
                    {"reasonCode": "self-service-complete", "sessionId": session.session_id}
                ),
            }
        )


@app.get("/api/v1/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "callflow-ai",
        "groq_enabled": bool(settings.groq_api_key),
        "twilio_signature_validation": settings.validate_twilio_signature,
        "human_handoff_configured": bool(settings.human_agent_phone),
    }


@app.post("/api/v1/simulate")
async def simulate(
    payload: SimulateRequest,
    x_api_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
) -> dict:
    _require_api_key(x_api_key, key)
    session_id = payload.session_id or f"SIM-{uuid.uuid4().hex[:16]}"
    session = live_sessions.get(session_id)
    if session is None:
        session = LiveSession(session_id=session_id, call_sid=session_id, caller=payload.caller)
        live_sessions[session_id] = session
        await asyncio.to_thread(store.start_call, session_id, session_id, payload.caller)
    return await _run_simulated_turn(session, payload.text)


@app.get("/api/v1/calls")
async def calls(
    x_api_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
) -> dict:
    _require_api_key(x_api_key, key)
    items = await asyncio.to_thread(store.recent_calls, limit)
    return {"items": items}


@app.get("/api/v1/metrics")
async def metrics(
    x_api_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
) -> dict:
    _require_api_key(x_api_key, key)
    raw = await asyncio.to_thread(store.metrics)
    recent = await asyncio.to_thread(store.recent_calls, 200)
    completed = raw.get("completed_calls", 0)
    handoffs = raw.get("handed_off_calls", 0)
    average_turns = round(
        sum(item.get("turn_count", 0) for item in recent) / max(raw.get("total_calls", 0), 1), 1
    )
    containment = round(((completed - handoffs) / max(completed, 1)) * 100, 1)
    return {
        **raw,
        "handoffs": handoffs,
        "appointments": raw.get("appointments_booked", 0),
        "containment_rate": max(containment, 0),
        "average_turns": average_turns,
    }


@app.post("/voice/incoming")
async def incoming_call(request: Request) -> Response:
    if not await valid_http_request(request, settings):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    form = await request.form()
    caller = str(form.get("From", "unknown"))
    xml = conversation_relay_twiml(
        settings.public_ws_url,
        f"{settings.public_base_url}/voice/connect-action",
        caller,
    )
    return Response(xml, media_type="application/xml")


@app.post("/voice/connect-action")
async def connect_action(request: Request) -> Response:
    if not await valid_http_request(request, settings):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    form = await request.form()
    raw_handoff = str(form.get("HandoffData", "") or "")
    try:
        handoff = json.loads(raw_handoff) if raw_handoff else {}
    except json.JSONDecodeError:
        handoff = {}
    session_status = str(form.get("SessionStatus", "")).casefold()
    reason_code = str(handoff.get("reasonCode", ""))
    session_id = str(handoff.get("sessionId") or form.get("SessionId") or "")

    is_live_handoff = reason_code == "live-agent-handoff"
    is_relay_failure = session_status in {"failed", "error"}
    if not is_live_handoff and not is_relay_failure:
        return Response(
            call_result_twiml("Thank you for calling. Goodbye."),
            media_type="application/xml",
        )

    reason = str(
        handoff.get("reason")
        or ("conversation_relay_failure" if is_relay_failure else "human requested")
    )
    if is_relay_failure and session_id:
        try:
            await asyncio.to_thread(store.request_handoff, session_id, reason)
        except (KeyError, ValueError):
            pass

    if not settings.human_agent_phone:
        saved = await _save_callback_request(session_id, reason, "agent_not_configured")
        message = (
            "Our team is unavailable. Your callback request has been saved."
            if saved
            else "Our team is unavailable right now. Please call again shortly."
        )
        return Response(call_result_twiml(message), media_type="application/xml")

    whisper_query = urlencode({"session_id": session_id, "reason": reason})
    dial_query = urlencode({"session_id": session_id, "reason": reason})
    xml = warm_handoff_twiml(
        settings.human_agent_phone,
        f"{settings.public_base_url}/voice/agent-whisper?{whisper_query}",
        f"{settings.public_base_url}/voice/dial-action?{dial_query}",
        "Our team is unavailable right now. Please call again shortly.",
    )
    return Response(xml, media_type="application/xml")


@app.post("/voice/agent-whisper")
async def agent_whisper(
    request: Request,
    session_id: str = Query(default="", max_length=160),
    reason: str = Query(default="human requested", max_length=160),
) -> Response:
    if not await valid_http_request(request, settings):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    call = await asyncio.to_thread(store.get_call, session_id) if session_id else None
    summary = call.get("summary", "") if call else "Conversation context was not available."
    return Response(agent_whisper_twiml(summary, reason), media_type="application/xml")


@app.post("/voice/dial-action")
async def dial_action(
    request: Request,
    session_id: str = Query(default="", max_length=160),
    reason: str = Query(default="human requested", max_length=160),
) -> Response:
    """Complete a warm transfer or create a real callback task on failure."""

    if not await valid_http_request(request, settings):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    form = await request.form()
    dial_status = str(form.get("DialCallStatus", "unknown")).casefold()
    if dial_status == "completed":
        call = await asyncio.to_thread(store.get_call, session_id) if session_id else None
        if call is not None:
            summary = call.get("summary") or f"Warm handoff completed: {reason}."
            completed = await asyncio.to_thread(
                store.finish_call,
                session_id,
                "handoff_completed",
                summary,
                reason,
            )
            _schedule_post_call(completed)
        return Response(call_result_twiml(), media_type="application/xml")

    saved = await _save_callback_request(session_id, reason, dial_status)
    message = (
        "The specialist could not answer. Your callback request has been saved."
        if saved
        else "The specialist could not answer. Please call again shortly."
    )
    return Response(call_result_twiml(message), media_type="application/xml")


@app.websocket("/ws/conversation")
async def conversation_socket(websocket: WebSocket) -> None:
    if not valid_websocket(websocket, settings):
        await websocket.close(code=1008, reason="Invalid Twilio signature")
        return
    await websocket.accept()
    session: LiveSession | None = None
    response_task: asyncio.Task | None = None
    try:
        while True:
            event = await websocket.receive_json()
            event_type = event.get("type")
            if event_type == "setup":
                session_id = str(event.get("sessionId") or f"VX-{uuid.uuid4().hex}")
                custom = event.get("customParameters") or {}
                session = LiveSession(
                    session_id=session_id,
                    call_sid=str(event.get("callSid", "")),
                    caller=str(event.get("from") or custom.get("caller") or "unknown"),
                )
                live_sessions[session_id] = session
                await asyncio.to_thread(
                    store.start_call, session.session_id, session.call_sid, session.caller
                )
                continue
            if session is None:
                await websocket.close(code=1002, reason="setup event required")
                return
            if session.handoff_in_progress and event_type in {"prompt", "interrupt", "dtmf"}:
                continue
            if event_type == "prompt":
                session.language = str(event.get("lang") or session.language)
                prompt = str(event.get("voicePrompt", "")).strip()
                if not event.get("last", False):
                    session.partial_prompt = prompt
                    continue
                session.partial_prompt = ""
                await _settle_response(response_task, cancel=True)
                if session.handoff_in_progress:
                    continue
                response_task = asyncio.create_task(_send_voice_turn(websocket, session, prompt))
            elif event_type == "interrupt":
                await _settle_response(response_task, cancel=True)
                response_task = None
                session.interrupted_responses += 1
                heard = str(event.get("utteranceUntilInterrupt", "")).strip()
                if heard:
                    session.add("assistant", f"{heard} [interrupted]")
                    await _persist_turn(session.session_id, "assistant", f"{heard} [interrupted]")
            elif event_type == "dtmf" and str(event.get("digit")) == "0":
                await _settle_response(response_task, cancel=True)
                if session.handoff_in_progress:
                    continue
                response_task = asyncio.create_task(
                    _send_voice_turn(websocket, session, "Please connect me to a human agent")
                )
            elif event_type == "error":
                session.handoff_reason = "conversation_relay_error"
    except WebSocketDisconnect:
        pass
    finally:
        await _settle_response(response_task, cancel=True)
        if session:
            existing = await asyncio.to_thread(store.get_call, session.session_id)
            should_finalize = (
                existing
                and existing.get("status") not in {"completed", "handoff_requested"}
                and session.handoff_reason != "conversation_relay_error"
            )
            if should_finalize:
                try:
                    await _finalize(session, "caller_disconnected", session.handoff_reason)
                except KeyError:
                    pass
            live_sessions.pop(session.session_id, None)


STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
