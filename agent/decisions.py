"""Decision records (design section 11.4) and their template explanations.

A record holds everything needed to understand one decision later: the
signals, the constraints that bound, the action, the alternatives, the
projected impact, the confidence and what the gate did with it.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from agent.intelligence.confidence import Confidence
from agent.intelligence.orders import PlannedOrder
from agent.intelligence.problem import Problem
from agent.intelligence.projection import ProjectionResult
from agent.sync.snapshot import Snapshot


def pretty(entity_id: str) -> str:
    name = entity_id.split("-", 1)[-1]
    return {"coxsbazar": "Cox's Bazar"}.get(name, name.title())


def hours(ticks: int | None, tick_minutes: int) -> float | None:
    return None if ticks is None else ticks * tick_minutes / 60.0


def explain(record: dict[str, Any], tick_minutes: int = 15) -> str:
    """Deterministic explanation. The API's LLM explainer falls back to this."""
    station, fuel = pretty(record["station"]), record["fuel"].lower()
    action, impact = record["action"], record["impact"]
    qty = sum(s["quantity"] for s in action["shipments"])
    depot = pretty(action["depot"])
    before, after = impact["p_stockout_before"], impact["p_stockout_after"]
    h = hours(impact.get("time_to_stockout_ticks"), tick_minutes)
    if h is not None and before >= 0.2:
        opening = f"{station} {fuel} is projected to run out in {h:.1f} h."
    else:
        opening = f"{station} {fuel} is drifting towards its safety stock."
    text = f"{opening} Sending {qty:,.0f} L from {depot} cuts stockout risk from {before:.0%} to {after:.0%}."
    binding = record.get("constraints_binding") or []
    if binding:
        text += " Limited by: " + ", ".join(b.replace("_", " ") for b in binding) + "."
    if record["gate"]["result"] == "review":
        text += " Waiting for operator approval: " + "; ".join(record["gate"]["reasons"]) + "."
    return text


def build_record(*, decision_id: str, cycle_id: str, snapshot: Snapshot, problem: Problem, mode: str,
                 policy: str, config_version: str, station: str, fuel: str, orders: list[PlannedOrder],
                 keys: list[str], no_action: ProjectionResult, with_plan: ProjectionResult,
                 alternatives: list[dict[str, Any]], binding: list[str], confidence: Confidence,
                 gate: dict[str, Any], active_events: list[dict[str, Any]]) -> dict[str, Any]:
    s = problem.stations.index(station)
    f = problem.fuels.index(fuel)
    mu, sd = problem.mu[s, f, :8], problem.sd[s, f, :8]
    record: dict[str, Any] = {
        "decision_id": decision_id,
        "cycle_id": cycle_id,
        "tick": snapshot.tick,
        "planned_land_tick": problem.t0 + problem.lag,
        "mode": mode,
        "policy": policy,
        "config_version": config_version,
        "station": station,
        "fuel": fuel,
        "signals": {
            "inventory_l": round(float(problem.inv0[s, f]), 1),
            "capacity_l": float(problem.cap[s, f]),
            "in_transit_l": round(float(problem.in_transit_total[s, f]), 1),
            "data_age_ticks": int(problem.meta.get("data_age", 0)),
            "forecast_next_8_ticks_l": {
                "p50": round(float(mu.sum()), 1),
                "p90": round(float(mu.sum() + 1.2816 * np.sqrt((sd ** 2).sum())), 1),
            },
            "active_events": active_events,
            "route_redundancy": int(problem.available_routes[s]),
        },
        "constraints_binding": binding,
        "action": {
            "depot": orders[0].depot_id,
            "route": orders[0].route_id,
            "quantity_l": sum(o.quantity for o in orders),
            "shipments": [{"route": o.route_id, "quantity": o.quantity, "idempotency_key": k}
                          for o, k in zip(orders, keys)],
            "cross_region": any(o.cross_region for o in orders),
        },
        "alternatives": alternatives,
        "impact": {
            "p_stockout_before": round(no_action.p_stockout, 3),
            "p_stockout_after": round(with_plan.p_stockout, 3),
            "unmet_l_before": round(no_action.expected_unmet, 1),
            "unmet_l_after": round(with_plan.expected_unmet, 1),
            "time_to_stockout_ticks": no_action.time_to_stockout_ticks,
        },
        "confidence": {"value": round(confidence.value, 3), **confidence.to_dict()},
        "gate": gate,
        "outcome": {"status": "PENDING_GATE" if gate["result"] == "review" else "SENDING"},
    }
    record["explanation"] = explain(record, snapshot.tick_minutes)
    return record
