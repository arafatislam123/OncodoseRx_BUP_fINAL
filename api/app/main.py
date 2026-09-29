"""FastAPI app for the operator console. Run with: uvicorn app.main:app --port 8080"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.agent_client import agent
from app.config import settings
from app.db import db
from app.routes import router, sim_http
from app.telemetry import MetricsMiddleware


def _configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso", utc=True), structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    structlog.contextvars.bind_contextvars(service="api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _configure_logging()
    await db.open()
    yield
    await agent.close()
    await sim_http.aclose()
    await db.close()


app = FastAPI(title="BUP Fuel Supply Agent API", version="1.0.0", lifespan=lifespan,
              description="Operator API for the simulated fuel network. All data is simulated.")
app.add_middleware(MetricsMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=[settings.web_origin], allow_methods=["*"],
                   allow_headers=["Authorization", "Content-Type"])
app.include_router(router)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _compact(state: dict[str, Any]) -> dict[str, Any]:
    status = state.get("status", {})
    return {
        "type": "state",
        "tick": state.get("tick"),
        "mode": status.get("mode"),
        "autonomy": status.get("autonomy"),
        "service_level": (state.get("metrics") or {}).get("service_level"),
        "open_alerts": sum(1 for a in state.get("alerts", []) if a.get("state") == "open"),
        "pending_recommendations": sum(1 for r in state.get("recommendations", []) if r.get("state") == "pending"),
        "incident": state.get("incident"),
    }


@app.websocket("/api/ws")
async def ws(socket: WebSocket) -> None:
    """Pushes a small status message every second; the console refetches details when it changes."""
    await socket.accept()
    try:
        while True:
            state, live = await agent.state()
            await socket.send_json({**_compact(state or {}), "live": live})
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        return
