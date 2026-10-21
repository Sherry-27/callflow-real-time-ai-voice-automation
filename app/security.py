"""Twilio request verification helpers."""

from __future__ import annotations

from fastapi import Request, WebSocket
from twilio.request_validator import RequestValidator

from .config import Settings


def _external_http_url(request: Request, config: Settings) -> str:
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{config.public_base_url}{request.url.path}{query}"


async def valid_http_request(request: Request, config: Settings) -> bool:
    """Validate a form-encoded Twilio webhook using its exact public URL."""
    if not config.validate_twilio_signature:
        return True
    signature = request.headers.get("x-twilio-signature", "")
    if not signature or not config.twilio_auth_token:
        return False
    form = await request.form()
    return RequestValidator(config.twilio_auth_token).validate(
        _external_http_url(request, config), form, signature
    )


def valid_websocket(websocket: WebSocket, config: Settings) -> bool:
    """Validate the ConversationRelay WebSocket handshake signature."""
    if not config.validate_twilio_signature:
        return True
    signature = websocket.headers.get("x-twilio-signature", "")
    if not signature or not config.twilio_auth_token:
        return False
    validator = RequestValidator(config.twilio_auth_token)
    url = config.public_ws_url
    return validator.validate(url, {}, signature) or validator.validate(
        url.rstrip("/") + "/", {}, signature
    )


def valid_api_key(value: str | None, config: Settings) -> bool:
    return bool(value) and value == config.api_key
