"""Order-up-to heuristic (design section 8.4).

Used when the LP fails, times out, or the operator pins it, and as the
baseline the LP is compared against. Stations closest to running out are
served first. Each one gets topped up to cover lead time, the review period
and the safety stock, using the main route and a cross-region route only if
the main one is down.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from agent.intelligence.orders import PlanResult
from agent.intelligence.planner_lp import safety_stock
from agent.intelligence.problem import Problem


def _pick_route(problem: Problem, s: int) -> int | None:
    lag = problem.lag
    usable = [r for r in range(problem.R) if problem.route_dst[r] == s and problem.route_ok[r, lag]]
    same_region = [r for r in usable if not problem.cross[r]]
    pool = same_region or usable
    if not pool:
        return None
    return min(pool, key=lambda r: problem.route_transit[r])


def solve_heuristic(problem: Problem, policy: dict[str, Any] | None = None) -> PlanResult:
    p = policy or {}
    review = max(1, int(p.get("review_ticks", 8)))
    started = time.perf_counter()
    lag, H = problem.lag, problem.H
    ss = safety_stock(problem, float(p.get("ss_z", 1.2816)), float(p.get("ss_min_cover_ticks", 4)))

    dispatch_left = problem.dispatch_cap[:, lag].copy()
    depot_left = problem.depot_inv0 + problem.supply[:, :, :lag].sum(axis=2)
    headroom = problem.headroom_now().copy()

    candidates: list[tuple[float, int, int, int, float]] = []   # (ttso, s, f, route, need)
    for s in range(problem.S):
        r = _pick_route(problem, s)
        if r is None:
            continue
        land = min(problem.arrival_index(r, lag), H - 1)
        for f in range(problem.F):
            net = problem.intransit[s, f] - problem.mu[s, f]
            running = problem.inv0[s, f] + np.cumsum(net)
            below = np.nonzero(running < 0)[0]
            ttso = float(below[0]) if below.size else float(H)
            projected = running[land]
            cover_end = min(H, land + 1 + review)
            reorder_point = ss[s, f] + problem.mu[s, f, land + 1:cover_end].sum()
            if projected >= reorder_point:
                continue
            order_up_to = ss[s, f] + problem.mu[s, f, land + 1:min(H, land + 1 + 2 * review)].sum()
            candidates.append((ttso, s, f, r, order_up_to - projected))

    x: dict[tuple[int, int, int], float] = {}
    for _, s, f, r, need in sorted(candidates):
        d = problem.route_src[r]
        qty = min(need, headroom[s, f], depot_left[d, f], dispatch_left[d])
        if qty <= 0:
            continue
        x[(r, f, lag)] = x.get((r, f, lag), 0.0) + qty
        headroom[s, f] -= qty
        depot_left[d, f] -= qty
        dispatch_left[d] -= qty

    return PlanResult("heuristic", "heuristic", time.perf_counter() - started, x=x, safety_stock=ss)
