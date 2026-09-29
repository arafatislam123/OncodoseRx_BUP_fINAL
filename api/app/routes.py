"""Public /api routes for the operator console (design section 16)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.agent_client import AgentUnavailable, agent
from app.config import settings
from app.db import db
from app.explainer import explainer, state_template
from app.security import require_operator
from app.telemetry import window
from app.whatif import WhatIfRequest, run_whatif

router = APIRouter(prefix="/api")
POLICY_FILE = Path(__file__).resolve().parents[2] / "config" / "policy.default.yaml"
sim_http = httpx.AsyncClient(base_url=settings.simulator_url, timeout=5.0)


def _banner(live: bool, state: dict[str, Any] | None) -> dict[str, Any]:
    if live:
        return {"live": True}
    return {"live": False, "message": "Decision engine unreachable. Showing the last known state."
            if state else "Decision engine unreachable and no state has been received yet."}


# --- health and state --------------------------------------------------------
@router.get("/health")
async def health() -> dict[str, Any]:
    status_task = asyncio.create_task(agent.status())
    db_task = asyncio.create_task(db.ping())
    try:
        sim = await sim_http.get("/v1/health", timeout=1.5)
        sim_ok = sim.status_code == 200
    except httpx.HTTPError:
        sim_ok = False
    status, db_ok = await status_task, await db_task
    components = (status or {}).get("components", {})
    fuel_sim = components.get("fuel_simulator", "healthy") if sim_ok else "down"
    return {
        "backend_api": "healthy",
        "database": "healthy" if db_ok else ("down" if settings.database_url else "not_configured"),
        "fuel_simulator": fuel_sim,
        "prediction_service": components.get("prediction_service", "unknown") if status else "down",
        "decision_engine": components.get("decision_engine", "unknown") if status else "down",
        "sse": components.get("sse") or ("unknown" if status else "down"),
        "llm": explainer.status(),
        "mode": (status or {}).get("mode", "UNKNOWN"),
        "autonomy": (status or {}).get("autonomy"),
        "data_age_ticks": (status or {}).get("data_age_ticks"),
        "tick": (status or {}).get("tick"),
        "p95_latency_ms": window.p95_ms(),
        "error_rate": window.error_rate(),
        "agent": status,
    }


@router.get("/state")
async def state() -> dict[str, Any]:
    data, live = await agent.state()
    if data is None:
        raise HTTPException(503, "no state available yet: the decision engine hasn't completed a cycle")
    return {**data, "banner": _banner(live, data)}


@router.get("/forecast")
async def forecast(station: str, fuel: str, horizon: int = Query(32, ge=1, le=96)) -> dict[str, Any]:
    data, live = await agent.state()
    if data is None:
        raise HTTPException(503, "no state available yet")
    for st in data.get("stations", []):
        if st["id"] == station and fuel in st["fuels"]:
            fc = st["fuels"][fuel]["forecast"]
            history = await db.demand_series(station, fuel) or []
            return {"station": station, "fuel": fuel, "tick": data.get("tick"),
                    "forecast": {k: v[:horizon] for k, v in fc.items()},
                    "history": list(reversed(history)), "banner": _banner(live, data)}
    raise HTTPException(404, "unknown station or fuel")


@router.get("/alerts")
async def alerts(state: str | None = Query(None, pattern="^(open|resolved)$")) -> dict[str, Any]:
    data, live = await agent.state()
    items = (data or {}).get("alerts")
    if items is None:
        items = await db.alerts(state) or []
    elif state:
        items = [a for a in items if a["state"] == state]
    return {"alerts": items, "banner": _banner(live, data)}


@router.get("/incidents")
async def incidents() -> dict[str, Any]:
    rows = await db.incidents()
    if rows is None:
        data, _ = await agent.state()
        rows = (data or {}).get("incidents", [])
    return {"incidents": rows}


@router.post("/incidents/summary")
async def incident_summary() -> dict[str, Any]:
    data, _ = await agent.state()
    incident = (data or {}).get("incident")
    if not incident:
        return {"text": "No incident is open.", "source": "template"}
    context = {"incident": incident, "alerts": [a for a in data.get("alerts", []) if a["id"] in incident["alert_ids"]],
               "mode_transitions": data.get("mode_transitions", [])[:10]}
    text, source = await explainer.summarize("incident", context, incident.get("summary", ""))
    return {"text": text, "source": source}


@router.get("/summary")
async def summary() -> dict[str, Any]:
    data, _ = await agent.state()
    if data is None:
        raise HTTPException(503, "no state available yet")
    template = state_template(data)
    context = {"tick": data.get("tick"), "status": data.get("status"), "metrics": data.get("metrics"),
               "alerts": [a for a in data.get("alerts", []) if a["state"] == "open"][:15],
               "runways": data.get("runways")}
    text, source = await explainer.summarize("state", context, template)
    return {"text": text, "source": source}


# --- recommendations and decisions ---------------------------------------------
@router.get("/recommendations")
async def recommendations(state: str | None = None) -> dict[str, Any]:
    data, live = await agent.state()
    items = (data or {}).get("recommendations", [])
    if state:
        items = [r for r in items if r["state"] == state]
    return {"recommendations": items, "banner": _banner(live, data)}


class ReviewBody(BaseModel):
    note: str | None = Field(None, max_length=500)
    reviewer: str = Field("operator", max_length=80)


@router.post("/recommendations/{rec_id}/{action}")
async def review(rec_id: str, action: str, body: ReviewBody, _: str = Depends(require_operator)) -> dict[str, Any]:
    if action not in ("approve", "reject"):
        raise HTTPException(404, "unknown action")
    try:
        code, payload = await agent.send("POST", f"/control/recommendations/{rec_id}/{action}",
                                         body.model_dump())
    except AgentUnavailable:
        raise HTTPException(503, "decision engine unreachable; try again shortly")
    if code >= 400:
        raise HTTPException(code, (payload or {}).get("detail", "request failed"))
    agent.last_state_wall = 0.0   # show the change on the next read
    return payload


@router.get("/decisions")
async def decisions(limit: int = Query(50, ge=1, le=500), station: str | None = None, fuel: str | None = None,
                    gate: str | None = None, before_tick: int | None = None) -> dict[str, Any]:
    rows = await db.decisions(limit, station, fuel, gate, before_tick)
    source = "database"
    if rows is None:
        data, _ = await agent.state()
        rows = [d for d in (data or {}).get("decisions", [])
                if (not station or d["station"] == station) and (not fuel or d["fuel"] == fuel)
                and (not gate or d["gate"]["result"] == gate)][:limit]
        source = "agent_memory"
    return {"decisions": rows, "source": source}


@router.get("/decisions/{decision_id}")
async def decision(decision_id: str) -> dict[str, Any]:
    record = await db.decision(decision_id)
    if record is None:
        data, _ = await agent.state()
        record = next((d for d in (data or {}).get("decisions", []) if d["decision_id"] == decision_id), None)
    if record is None:
        raise HTTPException(404, "decision not found")
    text, source = await explainer.explain_decision(record)
    return {**record, "explanation": text, "explanation_source": source,
            "template_explanation": record.get("explanation")}


# --- what-if ------------------------------------------------------------------
@router.post("/whatif")
async def whatif(request: WhatIfRequest) -> dict[str, Any]:
    try:
        payload = await agent.problem()
    except AgentUnavailable as exc:
        raise HTTPException(503, str(exc))
    data, _ = await agent.state(max_age_s=5.0)
    regions = {s["id"]: s["region_id"] for s in (data or {}).get("stations", [])}
    # the LP and Monte Carlo are CPU work; keep them off the event loop
    return await asyncio.to_thread(run_whatif, payload, request, regions)


# --- agent control ------------------------------------------------------------------
class ModeBody(BaseModel):
    mode: str = Field(pattern="^(MANUAL|AUTO)$")


class AutonomyBody(BaseModel):
    autonomy: str = Field(pattern="^(autopilot|copilot|manual)$")


async def _forward(method: str, path: str, body: dict[str, Any]) -> Any:
    try:
        code, payload = await agent.send(method, path, body)
    except AgentUnavailable:
        raise HTTPException(503, "decision engine unreachable")
    if code >= 400:
        raise HTTPException(code, (payload or {}).get("detail", "request failed"))
    agent.last_state_wall = 0.0
    return payload


@router.get("/agent/mode")
async def get_mode() -> dict[str, Any]:
    status = await agent.status()
    if status is None:
        raise HTTPException(503, "decision engine unreachable")
    return {"mode": status["mode"], "autonomy": status["autonomy"], "planner_policy": status["planner_policy"]}


@router.put("/agent/mode")
async def put_mode(body: ModeBody, _: str = Depends(require_operator)) -> Any:
    return await _forward("PUT", "/control/mode", {"mode": body.mode, "who": "operator"})


@router.get("/agent/autonomy")
async def get_autonomy() -> dict[str, Any]:
    return await get_mode()


@router.put("/agent/autonomy")
async def put_autonomy(body: AutonomyBody, _: str = Depends(require_operator)) -> Any:
    return await _forward("PUT", "/control/autonomy", body.model_dump())


# --- policies -------------------------------------------------------------------
def _default_policy() -> dict[str, Any]:
    return yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")) if POLICY_FILE.exists() else {}


class PolicyBody(BaseModel):
    version: str = Field(max_length=60)
    config: dict[str, Any] | None = None
    planner_policy: str | None = Field(None, pattern="^(lp|heuristic)$")


@router.get("/policies")
async def policies() -> dict[str, Any]:
    rows = await db.policies()
    status = await agent.status()
    return {"policies": rows or [{"version": _default_policy().get("version"), "config": _default_policy(),
                                  "active": True}],
            "active": (status or {}).get("policy_version"), "planner_policy": (status or {}).get("planner_policy")}


@router.put("/policies/active")
async def activate_policy(body: PolicyBody, _: str = Depends(require_operator)) -> Any:
    config = body.config
    if config is None:
        rows = await db.policies() or []
        match = next((r for r in rows if r["version"] == body.version), None)
        if match is None and body.version == _default_policy().get("version"):
            config = _default_policy()
        elif match is None:
            raise HTTPException(404, f"unknown policy version {body.version}")
        else:
            config = match["config"]
    else:
        try:
            await db.save_policy(body.version, config, active=False)
        except Exception:
            pass   # still apply it; history just won't have it
    result = await _forward("PUT", "/control/policy", {"version": body.version, "config": config,
                                                       "planner_policy": body.planner_policy})
    try:
        await db.activate_policy(body.version)
    except Exception:
        pass
    return result


# --- chaos (demo only) --------------------------------------------------------------
class ChaosEvent(BaseModel):
    type: str = Field(pattern="^(demand_spike|route_disruption|station_outage|depot_constraint|shipment_delay|supply_shortfall)$")
    start_tick: int | None = Field(None, ge=0)
    start_in_ticks: int = Field(0, ge=0, le=500)
    duration_ticks: int = Field(16, gt=0, le=1000)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ChaosFault(BaseModel):
    type: str = Field(pattern="^(latency|unavailable|error_rate|stale_data|stream_disconnect)$")
    duration_seconds: int = Field(30, gt=0, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)


async def _admin(method: str, path: str, body: Any = None) -> Any:
    try:
        resp = await sim_http.request(method, f"/admin{path}", json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"simulator unreachable: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:300])
    return resp.json() if resp.content else {"status": "ok"}


@router.post("/chaos/events")
async def chaos_event(body: ChaosEvent, _: str = Depends(require_operator)) -> Any:
    start = body.start_tick
    if start is None:
        health = (await sim_http.get("/v1/health")).json()
        start = int(health["simulation"]["tick"]) + body.start_in_ticks
    return await _admin("POST", "/events", {"type": body.type, "start_tick": start,
                                            "duration_ticks": body.duration_ticks, "parameters": body.parameters})


@router.post("/chaos/faults")
async def chaos_fault(body: ChaosFault, _: str = Depends(require_operator)) -> Any:
    return await _admin("POST", "/faults", body.model_dump())


@router.post("/chaos/clear")
async def chaos_clear(_: str = Depends(require_operator)) -> Any:
    return await _admin("POST", "/faults/clear")


@router.post("/chaos/sim/{action}")
async def chaos_sim(action: str, _: str = Depends(require_operator)) -> Any:
    if action not in ("run", "pause", "step", "reset"):
        raise HTTPException(404, "unknown action")
    return await _admin("POST", f"/{action}")


@router.get("/chaos/timeline")
async def chaos_timeline() -> dict[str, Any]:
    faults, events = await asyncio.gather(_admin("GET", "/faults"), _admin("GET", "/events"),
                                          return_exceptions=True)
    return {"faults": faults if not isinstance(faults, Exception) else [],
            "events": events if not isinstance(events, Exception) else []}


# --- ops assistant (read-only) -------------------------------------------------------------
class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@router.post("/assistant")
async def assistant(body: Question) -> dict[str, Any]:
    data, live = await agent.state()
    if data is None:
        raise HTTPException(503, "no state available yet")
    words = {w.strip("?.,").lower() for w in body.question.split()}
    related = [a for a in data.get("alerts", []) if a["state"] == "open"
               and any(w in a["message"].lower() for w in words if len(w) > 3)]
    decisions_ = [d for d in data.get("decisions", [])
                  if any(w in d["station"] or w == d["fuel"].lower() for w in words if len(w) > 3)][:5]
    template = state_template(data)
    if related:
        template += " Related alerts: " + " ".join(a["message"] for a in related[:3])
    if decisions_:
        template += " Latest related decision: " + decisions_[0].get("explanation", "")
    context = {"status": data.get("status"), "metrics": data.get("metrics"), "tick": data.get("tick"),
               "stations": [{"id": s["id"], "status": s["status"],
                             "fuels": {f: {"inventory": v["inventory"], "cover_hours": v["cover_hours"],
                                           "risk": v["no_action"]["p_stockout"]} for f, v in s["fuels"].items()}}
                            for s in data.get("stations", [])],
               "open_alerts": [a for a in data.get("alerts", []) if a["state"] == "open"][:20],
               "recent_decisions": [{k: d.get(k) for k in ("decision_id", "station", "fuel", "action", "impact",
                                                            "gate", "explanation")}
                                    for d in data.get("decisions", [])[:10]],
               "runways": data.get("runways"), "events": data.get("events")}
    text, source = await explainer.answer(body.question, context, template)
    return {"answer": text, "source": source, "live": live}
