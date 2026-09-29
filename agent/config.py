"""Settings for the agent, read from environment variables and the files in config/."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("FSA_CONFIG_DIR", ROOT / "config"))


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Settings:
    simulator_url: str = field(default_factory=lambda: _env("SIMULATOR_BASE_URL", "http://localhost:8000"))
    simulation_speed: float = field(default_factory=lambda: float(_env("SIMULATION_SPEED", "8")))
    tick_minutes: int = field(default_factory=lambda: int(_env("TICK_MINUTES", "15")))
    autonomy: str = field(default_factory=lambda: _env("AGENT_AUTONOMY", "copilot"))
    planner_policy: str = field(default_factory=lambda: _env("PLANNER_POLICY", "lp"))
    horizon_ticks: int = field(default_factory=lambda: int(_env("PLANNER_HORIZON_TICKS", "32")))
    approval_ttl_ticks: int = field(default_factory=lambda: int(_env("APPROVAL_TTL_TICKS", "8")))
    confidence_threshold: float = field(default_factory=lambda: float(_env("CONFIDENCE_REVIEW_THRESHOLD", "0.6")))
    data_dir: Path = field(default_factory=lambda: Path(_env("AGENT_DATA_DIR", str(ROOT / "data"))))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))
    http_port: int = field(default_factory=lambda: int(_env("AGENT_PORT", "9100")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    # "live" follows the running clock; "stepped" plans once per tick and never skips
    run_mode: str = field(default_factory=lambda: _env("AGENT_RUN_MODE", "live"))

    policy: dict[str, Any] = field(default_factory=lambda: load_yaml("policy.default.yaml"))
    prior: dict[str, Any] = field(default_factory=lambda: load_yaml("world_prior.yaml"))
    facts: dict[str, Any] = field(default_factory=lambda: load_yaml("world_facts.yaml"))

    @property
    def ticks_per_day(self) -> int:
        return int(24 * 60 / self.tick_minutes)

    def p(self, section: str, key: str, default: Any = None) -> Any:
        """Read one value from the active policy."""
        return self.policy.get(section, {}).get(key, default)
