"""Talks to the agent's internal API.

The last good state is kept in memory, so when the agent is down the console
still shows what we last knew, with a banner, instead of an error page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.config import settings


class AgentUnavailable(Exception):
    pass


class AgentClient:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.AsyncClient(base_url=base_url, timeout=2.0)
        self.last_state: dict[str, Any] | None = None
        self.last_state_wall = 0.0
        self.last_problem: dict[str, Any] | None = None
        self.last_problem_wall = 0.0
        self._state_lock = asyncio.Lock()
        self.reachable = False

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str) -> Any:
        try:
            resp = await self.http.get(path)
        except httpx.HTTPError as exc:
            self.reachable = False
            raise AgentUnavailable(str(exc)) from exc
        self.reachable = True
        if resp.status_code >= 500:
            raise AgentUnavailable(f"agent returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    async def state(self, max_age_s: float = 0.5) -> tuple[dict[str, Any] | None, bool]:
        """Returns (state, live). Many dashboard requests share one agent call per half second."""
        async with self._state_lock:
            if self.last_state is not None and time.monotonic() - self.last_state_wall < max_age_s:
                return self.last_state, True
            try:
                self.last_state = await self._get("/state")
                self.last_state_wall = time.monotonic()
                return self.last_state, True
            except AgentUnavailable:
                return self.last_state, False

    async def status(self) -> dict[str, Any] | None:
        try:
            return await self._get("/status")
        except AgentUnavailable:
            return None

    async def problem(self, max_age_s: float = 1.0) -> dict[str, Any]:
        if self.last_problem is not None and time.monotonic() - self.last_problem_wall < max_age_s:
            return self.last_problem
        try:
            self.last_problem = await self._get("/problem")
            self.last_problem_wall = time.monotonic()
        except (AgentUnavailable, httpx.HTTPStatusError):
            if self.last_problem is None:
                raise AgentUnavailable("no planning inputs available yet")
        return self.last_problem

    async def send(self, method: str, path: str, json: Any = None) -> tuple[int, Any]:
        try:
            resp = await self.http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            self.reachable = False
            raise AgentUnavailable(str(exc)) from exc
        self.reachable = True
        body = resp.json() if resp.content else None
        return resp.status_code, body


agent = AgentClient(settings.agent_url)
