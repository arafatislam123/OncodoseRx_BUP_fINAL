"""The agent's internal HTTP interface on :9100.

Only reachable on the compose network. The api service uses it to read state
and to pass on operator commands (approvals, mode, autonomy, policy).
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from agent.core import Agent
from agent.intelligence.problem import problem_to_dict


class ReviewBody(BaseModel):
    reviewer: str = "operator"
    note: str | None = None


class ModeBody(BaseModel):
    mode: str            # MANUAL to take control, AUTO to hand it back
    who: str = "operator"


class AutonomyBody(BaseModel):
    autonomy: str


class PolicyBody(BaseModel):
    planner_policy: str | None = None
    version: str | None = None
    config: dict[str, Any] | None = None


def create_app(agent: Agent, sse_healthy: Callable[[], bool] | None = None) -> FastAPI:
    app = FastAPI(title="fsa agent", docs_url=None, redoc_url=None)

    def sse() -> bool | None:
        return sse_healthy() if sse_healthy else None

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "cycles": agent.cycles}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return agent.status(sse())

    @app.get("/state")
    async def state() -> dict[str, Any]:
        out = agent.state()
        out["status"] = agent.status(sse())
        return out

    @app.get("/problem")
    async def problem() -> dict[str, Any]:
        """Current planning inputs, used by the api's what-if endpoint."""
        if agent.view.problem is None:
            raise HTTPException(503, "no cycle has completed yet")
        return {"tick": agent.view.tick, "problem": problem_to_dict(agent.view.problem),
                "policy": agent.s.policy, "facts": agent.s.facts}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/control/recommendations")
    async def recommendations() -> list[dict[str, Any]]:
        return [r.to_dict() for r in agent.queue.items.values()]

    @app.post("/control/recommendations/{rec_id}/{action}")
    async def review(rec_id: str, action: str, body: ReviewBody) -> dict[str, Any]:
        if action not in ("approve", "reject"):
            raise HTTPException(404, "unknown action")
        rec = agent.review(rec_id, action == "approve", body.reviewer, body.note)
        if rec is None:
            raise HTTPException(409, "recommendation is not pending (it may have expired)")
        return rec.to_dict()

    @app.put("/control/mode")
    async def mode(body: ModeBody) -> dict[str, Any]:
        if body.mode.upper() not in ("MANUAL", "AUTO"):
            raise HTTPException(422, "mode must be MANUAL or AUTO")
        agent.set_manual(body.mode.upper() == "MANUAL", body.who)
        return {"mode": agent.modes.mode}

    @app.put("/control/autonomy")
    async def autonomy(body: AutonomyBody) -> dict[str, Any]:
        try:
            agent.set_autonomy(body.autonomy)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"autonomy": agent.autonomy}

    @app.put("/control/policy")
    async def policy(body: PolicyBody) -> dict[str, Any]:
        try:
            agent.set_policy(body.planner_policy, body.config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.version:
            agent.s.policy["version"] = body.version
        return {"planner_policy": agent.policy_name, "version": agent.s.policy.get("version")}

    @app.post("/control/cycle")
    async def cycle() -> dict[str, Any]:
        """Run one cycle now and wait for it. Used by the stepped harness."""
        view = await agent.run_cycle()
        return {"tick": view.tick if view else None, "orders": len(view.orders) if view else 0}

    return app
