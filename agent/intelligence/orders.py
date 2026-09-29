"""Plan results and the post-processing that turns liters into real shipments."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from agent.intelligence.problem import Problem


@dataclass
class PlannedOrder:
    route_id: str
    depot_id: str
    station_id: str
    fuel: str
    quantity: float
    create_tick: int
    part: int = 0
    cross_region: bool = False
    critical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class PlanResult:
    policy: str
    status: str                                   # optimal | feasible | heuristic | failed
    solve_seconds: float
    # liters by (route index, fuel index, slot) for the whole horizon
    x: dict[tuple[int, int, int], float] = field(default_factory=dict)
    binding: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    safety_stock: np.ndarray | None = None

    def first_period(self, lag: int) -> dict[tuple[int, int], float]:
        return {(r, f): v for (r, f, j), v in self.x.items() if j == lag and v > 1e-6}

    def total_by_station(self, problem: Problem, lag: int) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for (r, f), liters in self.first_period(lag).items():
            key = (problem.stations[problem.route_dst[r]], problem.fuels[f])
            out[key] = out.get(key, 0.0) + liters
        return out


def to_orders(problem: Problem, plan: PlanResult, critical: set[tuple[str, str]] | None = None,
              bucket: float = 250.0, min_shipment: float = 500.0) -> list[PlannedOrder]:
    """Turn first-slot liters into shipments.

    Amounts are rounded to 250 L steps (up when the tank has room, since the LP
    plans on median demand), small orders are lifted to min_shipment when there
    is room and dropped otherwise unless the station is critical, and anything
    over the route's max_shipment is split.
    """
    critical = critical or set()
    headroom = problem.headroom_now().copy()
    orders: list[PlannedOrder] = []
    for (r, f), liters in sorted(plan.first_period(problem.lag).items()):
        route = problem.routes[r]
        s = int(problem.route_dst[r])
        station = problem.stations[s]
        fuel = problem.fuels[f]
        is_critical = (station, fuel) in critical
        room = math.floor(headroom[s, f] / bucket) * bucket
        total = min(math.ceil(liters / bucket) * bucket, room)
        if total < min_shipment and liters >= 0.25 * min_shipment and room >= min_shipment:
            total = min_shipment
        if total <= 0 or (total < min_shipment and not is_critical):
            continue
        headroom[s, f] -= total
        pieces = max(1, math.ceil(total / route.max_shipment))
        size = math.floor(total / pieces / bucket) * bucket
        if size <= 0:
            continue
        remainder = total - size * pieces
        for part in range(pieces):
            qty = size + (remainder if part == 0 else 0)
            qty = min(qty, math.floor(route.max_shipment / bucket) * bucket)
            orders.append(PlannedOrder(
                route_id=route.id, depot_id=route.source_depot_id, station_id=station, fuel=fuel,
                quantity=float(qty), create_tick=problem.t0 + problem.lag, part=part,
                cross_region=bool(problem.cross[r]), critical=is_critical,
            ))
    return orders


def orders_to_x(problem: Problem, orders: list[PlannedOrder]) -> dict[tuple[int, int, int], float]:
    """Inverse of to_orders, used to project the rounded plan rather than the raw LP output."""
    r_idx = {route.id: i for i, route in enumerate(problem.routes)}
    f_idx = {fuel: i for i, fuel in enumerate(problem.fuels)}
    x: dict[tuple[int, int, int], float] = {}
    for order in orders:
        key = (r_idx[order.route_id], f_idx[order.fuel], order.create_tick - problem.t0)
        x[key] = x.get(key, 0.0) + order.quantity
    return x
