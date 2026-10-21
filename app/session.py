"""In-memory state for one live ConversationRelay connection."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LiveSession:
    session_id: str
    call_sid: str = ""
    caller: str = "unknown"
    language: str = "en"
    messages: list[dict[str, str]] = field(default_factory=list)
    partial_prompt: str = ""
    intent: str = "general"
    handoff_reason: str = ""
    handoff_in_progress: bool = False
    interrupted_responses: int = 0
    frustration_score: int = 0
    pending_fields: dict[str, str] = field(default_factory=dict)

    def add(self, role: str, text: str) -> None:
        cleaned = " ".join(text.split()).strip()
        if cleaned:
            self.messages.append({"role": role, "content": cleaned})
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]

    def transcript_text(self) -> str:
        return "\n".join(f"{item['role']}: {item['content']}" for item in self.messages)

    def short_summary(self) -> str:
        user_turns = [item["content"] for item in self.messages if item["role"] == "user"]
        if not user_turns:
            return "Call connected; no caller request captured."
        joined = " ".join(user_turns[-3:])
        return f"Caller intent: {self.intent}. Recent request: {joined[:360]}"
