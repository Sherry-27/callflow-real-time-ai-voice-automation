"""Best-effort post-call automation delivery."""

from __future__ import annotations

import httpx


async def send_post_call(webhook_url: str, payload: dict) -> bool:
    if not webhook_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False
