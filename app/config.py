"""Environment-backed configuration for CallFlow AI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load only this project's file; never walk upward into an unrelated workspace.
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=False)


def _truthy(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int
    database_path: str
    public_base_url: str
    public_ws_url: str
    groq_api_key: str
    groq_model: str
    twilio_auth_token: str
    validate_twilio_signature: bool
    human_agent_phone: str
    n8n_post_call_webhook: str
    api_key: str

    @classmethod
    def from_env(cls) -> Settings:
        base = os.getenv("PUBLIC_BASE_URL", "https://your-domain.example").rstrip("/")
        ws_default = base.replace("https://", "wss://").replace("http://", "ws://")
        return cls(
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "8001")),
            database_path=os.getenv("DATABASE_PATH", "data/callflow.db"),
            public_base_url=base,
            public_ws_url=os.getenv("PUBLIC_WS_URL", f"{ws_default}/ws/conversation"),
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
            groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            validate_twilio_signature=_truthy("VALIDATE_TWILIO_SIGNATURE", True),
            human_agent_phone=os.getenv("HUMAN_AGENT_PHONE", "").strip(),
            n8n_post_call_webhook=os.getenv("N8N_POST_CALL_WEBHOOK", "").strip(),
            api_key=os.getenv("API_KEY", "local-demo-key").strip(),
        )


settings = Settings.from_env()
