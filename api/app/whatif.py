"""What-if analysis (design sections 8.5 and 16).

Takes the agent's current planning inputs, applies a made-up event to a copy,
re-plans with the same LP, and projects "today" against "if this happened".
Nothing is ever sent to the simulator.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from agent.intelligence.orders import orders_to_x, to_orders
from agent.intelligence.planner_heuristic import solve_heuristic
from agent.intelligence.planner_lp import solve_lp
from agent.intelligence.problem import Problem, problem_from_dict
from agent.intelligence.projection import plan_arrivals, project

EventType = Literal["demand_spike", "route_disruption", "station_outage", "depot_constraint",
                    "shipment_delay", "supply_shortfall"]


class HypotheticalEvent(BaseModel):
    type: EventType
    start_in_ticks: int = Field(0, ge=0, le=96)
    duration_ticks: int = Field(16, gt=0, le=96)
    parameters: dict[str, Any] = Field(default_factory=dict)


class WhatIfRequest(BaseModel):
    events: list[HypotheticalEvent] = Field(min_length=1, max_length=5)
    samples: int = Field(200, ge=20, le=500)


def _match(ids: list[str] | None, value: str) -> bool:
    return not ids or value in ids


def apply_event(problem: Problem, event: HypotheticalEvent, regions: dict[str, str]) -> Problem:
    """Apply one event to a copy of the planning inputs. Slot/index windows follow problem.py."""
    p = copy.deepcopy(problem)
    params = event.parameters
    start, end = event.start_in_ticks, event.start_in_ticks + event.duration_ticks
    k_window = slice(max(0, start - 1), min(p.H, end - 1))          # demand indices for ticks t0+start..
    j_window = range(max(0, start), min(p.H, end))                  # creation slots

    if event.type == "demand_spike":
        mult = max(0.01, float(params.get("multiplier", 1.5)))
        for s, sid in enumerate(p.stations):
            if _match(params.get("station_ids"), sid) and _match(params.get("region_ids"), regions.get(sid, "")):
                p.mu[s, :, k_window] *= mult
                p.sd[s, :, k_window] *= mult
    elif event.type == "route_disruption":
        for r, route in enumerate(p.routes):
            if _match(params.get("route_ids"), route.id):
                for j in range(p.H):
                    created, departs = j, j + p.dd
                    if start <= created < end or start <= departs < end:
                        p.route_ok[r, j] = False
                p.available_routes[p.route_dst[r]] -= 1 if start == 0 else 0
    elif event.type == "station_outage":
        for s, sid in enumerate(p.stations):
            if _match(params.get("station_ids"), sid):
                p.mu[s, :, k_window] = 0.0
                p.sd[s, :, k_window] = 0.0
                for j in j_window:
                    p.station_open[s, j] = False
                    p.route_ok[p.route_dst == s, j] = False
    elif event.type == "depot_constraint":
        derate = float(params.get("derate", 0.5))
        for d, did in enumerate(p.depots):
            if _match(params.get("depot_ids"), did):
                for j in j_window:
                    p.dispatch_cap[d, j] *= derate
    elif event.type in ("shipment_delay", "supply_shortfall"):
        for d, did in enumerate(p.depots):
            if not _match(params.get("depot_ids"), did):
                continue
            for f, fuel in enumerate(p.fuels):
                if not _match(params.get("fuel_types"), fuel):
                    continue
                if event.type == "supply_shortfall":
                    p.supply[d, f, start:] *= float(params.get("factor", 0.5))
                else:
                    delay = int(params.get("delay_ticks", 2))
                    shifted = np.zeros(p.H)
                    for j in range(start, p.H):
                        if j + delay < p.H:
                            shifted[j + delay] += p.supply[d, f, j]
                    shifted[:start] = p.supply[d, f, :start]
                    p.supply[d, f] = shifted
    return p


def _plan(problem: Problem, policy: dict[str, Any]) -> tuple[Any, list, str]:
    planner = policy.get("planner", {})
    plan = solve_lp(problem, planner, time_limit_ms=int(planner.get("time_limit_ms", 150)) * 2)
    used = "lp"
    if plan.status == "failed":
        plan, used = solve_heuristic(problem, planner), "heuristic"
    orders = to_orders(problem, plan, bucket=float(planner.get("bucket_liters", 250)),
                       min_shipment=float(planner.get("min_shipment", 500)))
    return plan, orders, used


def _full_x(problem: Problem, plan: Any, orders: list) -> dict:
    x = {k: v for k, v in plan.x.items() if k[2] != problem.lag}
    for key, value in orders_to_x(problem, orders).items():
        x[key] = x.get(key, 0.0) + value
    return x


def run_whatif(payload: dict[str, Any], request: WhatIfRequest, regions: dict[str, str]) -> dict[str, Any]:
    base = problem_from_dict(payload["problem"])
    policy = payload.get("policy", {})
    window = min(base.H, int(policy.get("projection", {}).get("risk_window_ticks", 16)) * 2)

    crisis = base
    for event in request.events:
        crisis = apply_event(crisis, event, regions)

    base_plan, _, _ = _plan(base, policy)
    crisis_plan, crisis_orders, used = _plan(crisis, policy)

    today = project(base, plan_arrivals(base, _full_x(base, base_plan, [])), samples=request.samples,
                    horizon=window)
    ignored = project(crisis, plan_arrivals(crisis, _full_x(crisis, base_plan, [])), samples=request.samples,
                      horizon=window)
    adapted = project(crisis, plan_arrivals(crisis, _full_x(crisis, crisis_plan, crisis_orders)),
                      samples=request.samples, horizon=window)

    rows = []
    for key in sorted(today):
        rows.append({
            "station": key[0], "fuel": key[1],
            "today": today[key].to_dict(),
            "crisis_current_plan": ignored[key].to_dict(),
            "crisis_replanned": adapted[key].to_dict(),
        })
    total = lambda res: round(sum(r.expected_unmet for r in res.values()), 1)  # noqa: E731
    return {
        "tick": payload.get("tick"),
        "window_ticks": window,
        "planner": used,
        "events": [e.model_dump() for e in request.events],
        "summary": {
            "unmet_today": total(today),
            "unmet_if_we_keep_the_plan": total(ignored),
            "unmet_if_we_replan": total(adapted),
            "stations_at_risk": sum(1 for r in adapted.values() if r.p_stockout >= 0.2),
        },
        "rows": rows,
        "recommended_now": [o.to_dict() for o in crisis_orders],
    }
