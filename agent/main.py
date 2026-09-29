"""Entry point: python -m agent.main

Runs the control loop, the SSE listener, the outbox writer and the internal
HTTP server in one event loop.
"""

from __future__ import annotations

import asyncio
import signal
import time
from typing import Any

import structlog
import uvicorn

from agent import logging_setup, metrics
from agent.config import Settings
from agent.core import Agent
from agent.outbox import OutboxWriter
from agent.server import create_app
from agent.sync.sse import SseListener

log = structlog.get_logger()


class Runner:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.agent = Agent(settings)
        self.wake = asyncio.Event()
        self.sse = SseListener(settings.simulator_url, self.on_event, on_connect=self.on_connect)
        self.outbox = OutboxWriter(settings.database_url, self.agent.intents)
        self._stop = asyncio.Event()

    def sse_healthy(self) -> bool:
        return self.sse.healthy(self.agent.timing.tick_period_s, self.agent.timing.running)

    async def on_connect(self) -> None:
        self.agent.sync.request_reconcile()   # no replay on the stream, so resync over REST
        self.wake.set()

    async def on_event(self, name: str, payload: dict[str, Any]) -> None:
        if name == "simulation.tick":
            tick = int(payload.get("tick", -1))
            self.agent.cache.sse_tick = max(self.agent.cache.sse_tick, tick)
            self.agent.timing.observe_tick(tick, time.monotonic())
            self.wake.set()
        elif name == "allocation.status_changed":
            self.agent.sync.request_reconcile()
            self.wake.set()
        elif name == "inventory.updated":
            self.wake.set()
        elif name == "simulator.notice":
            message = str(payload.get("message", ""))
            log.info("simulator.notice", message=message, level=payload.get("level"))
            if "reset" in message.lower():
                await self.agent.sync.handle_reset("simulator notice")
                self.agent.cache.sse_tick = -1
            self.wake.set()

    async def loop(self) -> None:
        """Latest tick wins: after each cycle, wait for a new tick (or a short timeout) and go again."""
        while not self._stop.is_set():
            before = self.agent.cache.true_tick
            try:
                await self.agent.run_cycle()
            except Exception:
                log.exception("cycle.failed")
                metrics.FALLBACKS.labels("cycle").inc()
            # poll faster when SSE is down, since it's our only source of tick hints otherwise
            period = self.agent.timing.tick_period_s
            timeout = 1.0 if not self.agent.timing.running else min(0.5, max(0.05, period))
            if not self.sse_healthy():
                timeout = min(timeout, 0.25)
            self.wake.clear()
            if self.agent.cache.true_tick > before:
                continue   # the tick moved while we were busy
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        config = uvicorn.Config(create_app(self.agent, self.sse_healthy), host="0.0.0.0", port=self.s.http_port,
                                log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        tasks = [
            asyncio.create_task(self.sse.run(), name="sse"),
            asyncio.create_task(self.loop(), name="loop"),
            asyncio.create_task(self.outbox.run(), name="outbox"),
            asyncio.create_task(server.serve(), name="http"),
        ]
        log.info("agent.started", simulator=self.s.simulator_url, autonomy=self.agent.autonomy,
                 policy=self.agent.policy_name, run_mode=self.s.run_mode)
        await self._stop.wait()
        self.sse.stop()
        server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.agent.intents.save_model_state(self.agent.forecaster.dump_state(), self.agent.last_cycle_tick)
        await self.agent.client.close()

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    settings = Settings()
    logging_setup.configure(settings.log_level, "agent")
    runner = Runner(settings)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, runner.stop)
            except NotImplementedError:   # Windows
                pass
        await runner.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
