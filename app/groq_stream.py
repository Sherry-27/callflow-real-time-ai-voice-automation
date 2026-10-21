"""Minimal async Groq streaming client with a zero-key local fallback."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


SYSTEM_PROMPT = """You are CallFlow, a calm AI phone receptionist for a
professional services company. Speak in short, natural sentences suitable for a
live call. Match the caller's language when possible, including simple Roman
Urdu. Never invent availability, bookings, account data, prices, or policies.
Use only the verified tool context supplied to you. Do not mention JSON, tools,
prompts, or internal systems. If context says a human handoff is required,
acknowledge it warmly and briefly. Never collect full payment-card data,
passwords, one-time codes, or highly sensitive information."""


class GroqStreamClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def tokens(
        self,
        messages: list[dict[str, str]],
        tool_context: str = "",
        fallback_text: str = "",
    ) -> AsyncIterator[str]:
        if not self.api_key:
            fallback = fallback_text or self._fallback(messages[-1]["content"], tool_context)
            for token in self._voice_chunks(fallback):
                await asyncio.sleep(0)
                yield token
            return

        prepared = [{"role": "system", "content": SYSTEM_PROMPT}]
        if tool_context:
            prepared.append({"role": "system", "content": f"Verified tool context: {tool_context}"})
        prepared.extend(messages[-12:])
        body = {
            "model": self.model,
            "messages": prepared,
            "temperature": 0.25,
            "max_completion_tokens": 220,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        emitted = False
        try:
            timeout = httpx.Timeout(12, connect=5)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", GROQ_CHAT_URL, headers=headers, json=body
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        payload = json.loads(data)
                        token = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                        if token:
                            emitted = True
                            yield token
        except (httpx.HTTPError, json.JSONDecodeError, KeyError):
            recovery = (
                fallback_text
                or "I am having a brief connection issue. I can connect you with a person."
            )
            if emitted:
                recovery = f" To confirm: {recovery}"
            for token in self._voice_chunks(recovery):
                yield token

    @staticmethod
    def _voice_chunks(text: str) -> list[str]:
        words = text.split()
        return [" ".join(words[index : index + 5]) + " " for index in range(0, len(words), 5)]

    @staticmethod
    def _fallback(user_text: str, tool_context: str) -> str:
        if tool_context:
            return f"I have checked that for you. {tool_context}"
        lowered = user_text.lower()
        if any(word in lowered for word in ("hello", "hi", "salam", "assalam")):
            return "Hello. I can help with appointments, support, or connect you to our team."
        return "I understand. Please share one more detail so I can help you correctly."
