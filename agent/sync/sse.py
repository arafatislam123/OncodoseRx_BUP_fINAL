"""SSE listener for /v1/stream (design section 7.5).

SSE is only a hint. Every event makes the agent read the REST API sooner, and
the agent keeps polling when the stream is down.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
import structlog

from agent import metrics

log = structlog.get_logger()

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class SseState:
    connected: bool = False
    last_tick_wall: float = field(default_factory=time.monotonic)
    last_tick: int = -1
    reconnects: int = 0


def parse_sse(buffer: str) -> tuple[list[tuple[str, str]], str]:
    """Split raw stream text into (event, data) pairs. Returns leftover text too."""
    events: list[tuple[str, str]] = []
    while "\n\n" in buffer:
        block, buffer = buffer.split("\n\n", 1)
        name, data_lines = "message", []
        for line in block.splitlines():
            if line.startswith(":"):
                continue  # comment: ": connected" or ": keepalive"
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((name, "\n".join(data_lines)))
    return events, buffer


class SseListener:
    def __init__(self, base_url: str, handler: Handler,
                 on_connect: Callable[[], Awaitable[None]] | None = None) -> None:
        self.url = base_url.rstrip("/") + "/v1/stream"
        self.handler = handler
        self.on_connect = on_connect
        self.state = SseState()
        self._stop = asyncio.Event()
        # small local queue: repeated ticks get merged so we never fall 200 events behind
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=500)

    def stop(self) -> None:
        self._stop.set()

    def healthy(self, tick_period_s: float, running: bool) -> bool:
        if not self.state.connected:
            return False
        if not running:
            return True
        silence = time.monotonic() - self.state.last_tick_wall
        return silence <= max(3.0, 20 * tick_period_s)

    async def run(self) -> None:
        consumer = asyncio.create_task(self._consume())
        backoff = 0.5
        try:
            while not self._stop.is_set():
                try:
                    await self._stream_once()
                    backoff = 0.5
                except (httpx.HTTPError, ConnectionError, asyncio.TimeoutError) as exc:
                    log.info("sse.disconnected", error=str(exc)[:200])
                self._set_connected(False)
                if self._stop.is_set():
                    break
                self.state.reconnects += 1
                metrics.SSE_RECONNECTS.inc()
                await asyncio.sleep(backoff * random.uniform(0.7, 1.3))
                backoff = min(backoff * 2, 5.0)
        finally:
            consumer.cancel()

    async def _stream_once(self) -> None:
        timeout = httpx.Timeout(connect=3.0, read=30.0, write=3.0, pool=3.0)  # keepalive is every 15 s
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("GET", self.url) as resp:
                if resp.status_code != 200:
                    # 503 FAULT_INJECTED during a stream_disconnect fault
                    raise httpx.HTTPError(f"stream returned {resp.status_code}")
                self._set_connected(True)
                log.info("sse.reconnected", reconnects=self.state.reconnects)
                if self.on_connect:
                    await self.on_connect()  # no replay, so resync everything over REST
                buffer = ""
                async for chunk in resp.aiter_text():
                    if self._stop.is_set():
                        return
                    buffer += chunk
                    events, buffer = parse_sse(buffer)
                    for name, raw in events:
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            continue
                        if name == "simulation.tick":
                            self.state.last_tick_wall = time.monotonic()
                            self.state.last_tick = int(payload.get("tick", -1))
                        self._enqueue(name, payload)

    def _enqueue(self, name: str, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((name, payload))
        except asyncio.QueueFull:
            # drop the oldest; the next REST refresh fills in whatever we missed
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait((name, payload))

    async def _consume(self) -> None:
        while True:
            name, payload = await self._queue.get()
            # merge a run of tick events into the newest one
            if name == "simulation.tick":
                while not self._queue.empty():
                    nxt_name, nxt_payload = self._queue._queue[0]  # type: ignore[attr-defined]
                    if nxt_name != "simulation.tick":
                        break
                    self._queue.get_nowait()
                    payload = nxt_payload
            try:
                await self.handler(name, payload)
            except Exception:  # a handler bug must not kill the stream
                log.exception("sse.handler_failed", event_name=name)

    def _set_connected(self, value: bool) -> None:
        self.state.connected = value
        metrics.SSE_CONNECTED.set(1 if value else 0)
