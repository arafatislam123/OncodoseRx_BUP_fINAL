"""HTTP client for the simulator.

GETs get an adaptive timeout and at most two quick retries. POSTs are never
retried here: a POST that times out becomes an UNKNOWN intent and is settled
later by reconciling against /v1/allocations (design section 9).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from agent import metrics
from agent.sync.models import RESOURCES, AllocationRequest, Health


class SimError(Exception):
    """Base class for simulator call failures."""


class FaultInjected(SimError):
    pass


class SimTimeout(SimError):
    pass


class InvalidResponse(SimError):
    pass


class LatencyTracker:
    """Keeps the last few latencies so timeouts follow the current conditions."""

    def __init__(self, size: int = 200) -> None:
        self._samples: deque[float] = deque(maxlen=size)

    def add(self, seconds: float) -> None:
        self._samples.append(seconds)

    def quantile(self, q: float, default: float = 0.05) -> float:
        if not self._samples:
            return default
        ordered = sorted(self._samples)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


class ApiHealth:
    """Sliding window of call outcomes in wall-clock time, read by the mode controller."""

    def __init__(self, window_s: float = 10.0) -> None:
        self.window_s = window_s
        self._events: deque[tuple[float, bool]] = deque()
        self.last_success_wall: float = time.monotonic()
        self.last_stale_start: float | None = None

    def record(self, ok: bool) -> None:
        now = time.monotonic()
        self._events.append((now, ok))
        if ok:
            self.last_success_wall = now
        self._trim(now)

    def record_stale(self, stale: bool) -> None:
        if stale and self.last_stale_start is None:
            self.last_stale_start = time.monotonic()
        elif not stale:
            self.last_stale_start = None

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self.window_s:
            self._events.popleft()

    def error_ratio(self) -> float:
        self._trim(time.monotonic())
        if not self._events:
            return 0.0
        return sum(1 for _, ok in self._events if not ok) / len(self._events)

    def seconds_since_success(self) -> float:
        return time.monotonic() - self.last_success_wall

    def stale_for(self) -> float:
        return 0.0 if self.last_stale_start is None else time.monotonic() - self.last_stale_start


@dataclass
class GetResult:
    data: Any
    stale: bool
    latency: float


@dataclass
class PostResult:
    # "created" | "rejected" | "unknown" | "client_error"
    outcome: str
    status: int | None
    code: str | None
    body: dict[str, Any] | None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class SimClient:
    def __init__(self, base_url: str, health: ApiHealth | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.health = health or ApiHealth()
        self.latency = LatencyTracker()
        self.post_latency = LatencyTracker()
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport,
                                       limits=httpx.Limits(max_connections=20))

    async def close(self) -> None:
        await self._http.aclose()

    def get_timeout(self) -> float:
        return _clamp(3 * self.latency.quantile(0.95), 0.3, 2.0)

    def post_timeout(self) -> float:
        return _clamp(3 * self.latency.quantile(0.95), 0.5, 3.0)

    async def get(self, resource: str, params: dict[str, Any] | None = None,
                  retries: int = 2, deadline: float | None = None) -> GetResult:
        path, model, is_list = RESOURCES[resource]
        attempt = 0
        while True:
            try:
                return await self._get_once(resource, path, model, is_list, params)
            except InvalidResponse:
                raise  # a bad payload won't get better by asking again right away
            except SimError:
                attempt += 1
                out_of_time = deadline is not None and time.monotonic() > deadline
                if attempt > retries or out_of_time:
                    raise
                await asyncio.sleep(random.uniform(0.05, 0.15))

    async def _get_once(self, resource: str, path: str, model: Any, is_list: bool,
                        params: dict[str, Any] | None) -> GetResult:
        started = time.monotonic()
        try:
            resp = await self._http.get(path, params=params, timeout=self.get_timeout())
        except httpx.TimeoutException as exc:
            self._record(resource, "timeout", started, ok=False)
            raise SimTimeout(f"{resource} timed out") from exc
        except httpx.HTTPError as exc:
            self._record(resource, "conn_error", started, ok=False)
            raise SimError(f"{resource}: {exc}") from exc

        latency = self._record(resource, str(resp.status_code), started, ok=resp.status_code == 200)
        if resp.status_code == 503:
            raise FaultInjected(f"{resource}: 503")
        if resp.status_code != 200:
            raise SimError(f"{resource}: HTTP {resp.status_code}")

        stale = resp.headers.get("X-Simulator-Stale", "").lower() == "true"
        self.health.record_stale(stale)
        try:
            payload = resp.json()
            if is_list:
                if not isinstance(payload, list):
                    raise InvalidResponse(f"{resource}: expected a list")
                data = [model.model_validate(item) for item in payload]
            else:
                data = model.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            metrics.INVALID_RESPONSES.labels(resource).inc()
            raise InvalidResponse(f"{resource}: {exc}") from exc
        return GetResult(data=data, stale=stale, latency=latency)

    def _record(self, endpoint: str, code: str, started: float, ok: bool) -> float:
        elapsed = time.monotonic() - started
        metrics.SIM_REQUESTS.labels(endpoint, code).inc()
        metrics.SIM_LATENCY.labels(endpoint).observe(elapsed)
        if code not in ("timeout", "conn_error"):
            self.latency.add(elapsed)
        self.health.record(ok)
        return elapsed

    async def post_allocation(self, request: AllocationRequest) -> PostResult:
        started = time.monotonic()
        try:
            resp = await self._http.post("/v1/allocations", json=request.model_dump(),
                                         timeout=self.post_timeout())
        except httpx.HTTPError:
            self._record("allocations_post", "timeout", started, ok=False)
            return PostResult("unknown", None, None, None)

        self.post_latency.add(time.monotonic() - started)
        self._record("allocations_post", str(resp.status_code), started, ok=resp.status_code < 500)
        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code in (200, 201):
            return PostResult("created", resp.status_code, None, body)
        if resp.status_code >= 500:
            return PostResult("unknown", resp.status_code, "FAULT_INJECTED", body)
        code = None
        if isinstance(body, dict) and isinstance(body.get("detail"), dict):
            code = body["detail"].get("code")
        if resp.status_code == 422:
            return PostResult("client_error", 422, "VALIDATION", body)
        return PostResult("rejected", resp.status_code, code, body)

    async def cancel(self, allocation_id: int) -> PostResult:
        try:
            resp = await self._http.post(f"/v1/allocations/{allocation_id}/cancel", timeout=self.post_timeout())
        except httpx.HTTPError:
            return PostResult("unknown", None, None, None)
        body = resp.json() if resp.content else None
        if resp.status_code == 200:
            return PostResult("created", 200, None, body)
        code = body.get("detail", {}).get("code") if isinstance(body, dict) else None
        return PostResult("rejected", resp.status_code, code, body)

    async def sim_health(self) -> Health:
        resp = await self._http.get("/v1/health", timeout=2.0)
        resp.raise_for_status()
        return Health.model_validate(resp.json())

    async def admin(self, method: str, path: str, json: Any = None) -> Any:
        """Call an /admin endpoint. These bypass faults, so probes and the harness use them."""
        resp = await self._http.request(method, f"/admin{path}", json=json, timeout=10.0)
        resp.raise_for_status()
        return resp.json() if resp.content else None
