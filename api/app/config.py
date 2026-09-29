"""API settings, all from environment variables. Nothing secret is hard-coded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class ApiSettings:
    agent_url: str = field(default_factory=lambda: _env("AGENT_URL", "http://localhost:9100"))
    simulator_url: str = field(default_factory=lambda: _env("SIMULATOR_BASE_URL", "http://localhost:8000"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    operator_token: str = field(default_factory=lambda: _env("OPERATOR_TOKEN"))
    web_origin: str = field(default_factory=lambda: _env("WEB_ORIGIN", "http://localhost:3000"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER").lower())
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5-5"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider == "anthropic" and bool(self.llm_api_key)


settings = ApiSettings()
