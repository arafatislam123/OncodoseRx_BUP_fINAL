"""Rolling-horizon linear program over both depots (design section 8.4).

Minimises unmet liters (the same thing service_level measures) plus a few
smaller terms: a soft safety-stock floor, fuel stranded at stations, cross
region trips, shipment count, and minus the value of fuel still sitting at a
depot where it can go anywhere. Only the first slot is executed; the LP is
solved again every cycle.

Orders may only be created on a review grid (now, now + 8, ...). Without it
the LP happily sends 100 L every tick, since shipment count is only a small
linear cost. The model is built with Constraint.SetCoefficient rather than
Python expressions, which keeps build time well under the 150 ms budget.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import numpy as np
from ortools.linear_solver import pywraplp

from agent.intelligence.forecaster import Forecaster
from agent.intelligence.orders import PlanResult
from agent.intelligence.problem import Problem

OVERFLOW_PENALTY = 2.0


def safety_stock(problem: Problem, z: float = 1.2816, min_cover_ticks: float = 4.0) -> np.ndarray:
    """P90 lead-time demand margin per station/fuel, widened by stale-data uncertainty.

    Per-tick noise alone gives a very thin margin (tens of liters), so there is
    also a floor of a few ticks of average demand to absorb forecast bias.
    """
    ss = np.zeros((problem.S, problem.F))
    for s in range(problem.S):
        lead = [problem.dd + int(problem.route_transit[r]) for r in range(problem.R)
                if problem.route_dst[r] == s]
        lead_time = (min(lead) if lead else 4) + 4  # plus a short review period
        for f in range(problem.F):
            sigma = Forecaster.lead_time_sigma(problem.sd[s, f, :lead_time])
            statistical = z * float(np.sqrt(sigma ** 2 + problem.inv0_sd[s, f] ** 2))
            floor = min_cover_ticks * float(np.mean(problem.mu[s, f, :lead_time]))
            ss[s, f] = max(statistical, floor)
    return ss


def solve_lp(problem: Problem, policy: dict[str, Any] | None = None,
             time_limit_ms: int = 150) -> PlanResult:
    p = policy or {}
    w_unmet = float(p.get("w_unmet", 1.0))
    decay = float(p.get("w_decay", 0.995))
    lam_ss = float(p.get("lambda_ss", 0.3))
    lam_ss_single = float(p.get("lambda_ss_single_route", 0.6))
    lam_str = float(p.get("lambda_stranded", 0.05))
    lam_x = float(p.get("lambda_cross_region", 0.02))
    lam_n = float(p.get("lambda_shipments", 0.5))
    lam_dep = float(p.get("lambda_depot_value", 0.01))
    lookahead = int(p.get("stranded_lookahead_ticks", 16))
    review = max(1, int(p.get("review_ticks", 8)))
    z = float(p.get("ss_z", 1.2816))
    min_cover = float(p.get("ss_min_cover_ticks", 4))

    started = time.perf_counter()
    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        return PlanResult("lp", "failed", 0.0)
    solver.SetTimeLimit(int(time_limit_ms))
    inf = solver.infinity()
    H, S, F, D, R, lag = problem.H, problem.S, problem.F, problem.D, problem.R, problem.lag
    objective = solver.Objective()
    objective.SetMinimization()

    # --- decision variables: liters on route r, fuel f, created at slot j ---
    x: dict[tuple[int, int, int], Any] = {}
    lands: dict[tuple[int, int, int], list[Any]] = defaultdict(list)   # (s, f, k) -> vars
    for r in range(R):
        cost = lam_dep + lam_n / float(problem.route_max[r]) + (lam_x if problem.cross[r] else 0.0)
        # periodic review: orders can only be created every `review` slots, so an
        # order placed now has to cover demand until the next chance to ship
        for j in range(lag, H, review):
            k = problem.arrival_index(r, j)
            if not problem.route_ok[r, j] or k >= H:
                continue
            for f in range(F):
                var = solver.NumVar(0.0, inf, "")
                x[r, f, j] = var
                lands[int(problem.route_dst[r]), f, k].append(var)
                objective.SetCoefficient(var, cost)

    ss = safety_stock(problem, z, min_cover)
    weights = w_unmet * decay ** np.arange(H)
    headroom = problem.headroom_now()

    # --- stations -----------------------------------------------------------
    for s in range(S):
        routes_in = [r for r in range(R) if problem.route_dst[r] == s]
        reach = [problem.arrival_index(r, lag) for r in routes_in]
        first_reach = min(reach) if reach else H
        ss_weight = (lam_ss_single if problem.available_routes[s] <= 1 else lam_ss) / max(1, H - first_reach)
        for f in range(F):
            cap = float(problem.cap[s, f])
            prev = None
            for k in range(H):
                served = solver.NumVar(0.0, float(problem.mu[s, f, k]), "")
                over = solver.NumVar(0.0, inf, "")
                nxt = solver.NumVar(0.0, inf, "")
                objective.SetCoefficient(served, -float(weights[k]))
                objective.SetCoefficient(over, OVERFLOW_PENALTY)
                inflow = float(problem.intransit[s, f, k]) + (float(problem.inv0[s, f]) if prev is None else 0.0)

                # nxt = prev + inflow + arrivals - over - served
                balance = solver.Constraint(inflow, inflow)
                balance.SetCoefficient(nxt, 1.0)
                balance.SetCoefficient(served, 1.0)
                balance.SetCoefficient(over, 1.0)
                # level after arrivals must fit in the tank: prev + inflow + arrivals - over <= cap
                tank = solver.Constraint(-inf, cap - inflow)
                tank.SetCoefficient(over, -1.0)
                if prev is not None:
                    balance.SetCoefficient(prev, -1.0)
                    tank.SetCoefficient(prev, 1.0)
                for var in lands.get((s, f, k), ()):
                    balance.SetCoefficient(var, -1.0)
                    tank.SetCoefficient(var, 1.0)

                if k >= first_reach:  # short >= ss - nxt
                    short = solver.NumVar(0.0, inf, "")
                    floor = solver.Constraint(float(ss[s, f]), inf)
                    floor.SetCoefficient(short, 1.0)
                    floor.SetCoefficient(nxt, 1.0)
                    objective.SetCoefficient(short, ss_weight)
                prev = nxt

            # stranded >= S_H - (ss + tail)
            tail = float(np.mean(problem.mu[s, f])) * lookahead
            stranded = solver.NumVar(0.0, inf, "")
            ct = solver.Constraint(-(float(ss[s, f]) + tail), inf)
            ct.SetCoefficient(stranded, 1.0)
            ct.SetCoefficient(prev, -1.0)
            objective.SetCoefficient(stranded, lam_str)

            # tank space is checked when the order is created, against inventory now
            first = [x[r, f, lag] for r in routes_in if (r, f, lag) in x]
            if first:
                ct = solver.Constraint(-inf, float(headroom[s, f]))
                for var in first:
                    ct.SetCoefficient(var, 1.0)

    # --- depots -----------------------------------------------------------
    for d in range(D):
        routes_out = [r for r in range(R) if problem.route_src[r] == d]
        for j in range(lag, H):
            used = [x[r, f, j] for r in routes_out for f in range(F) if (r, f, j) in x]
            if used:
                ct = solver.Constraint(-inf, float(problem.dispatch_cap[d, j]))
                for var in used:
                    ct.SetCoefficient(var, 1.0)
        for f in range(F):
            # cumulative shipments up to slot j can't exceed stock plus supply landed before j
            available = float(problem.depot_inv0[d, f])
            shipped: list[Any] = []
            for j in range(H):
                shipped += [x[r, f, j] for r in routes_out if (r, f, j) in x]
                if shipped and any((r, f, j) in x for r in routes_out):
                    ct = solver.Constraint(-inf, available)
                    for var in shipped:
                        ct.SetCoefficient(var, 1.0)
                available += float(problem.supply[d, f, j])

    status = solver.Solve()
    elapsed = time.perf_counter() - started
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return PlanResult("lp", "failed", elapsed)

    values = {key: var.solution_value() for key, var in x.items() if var.solution_value() > 1e-6}
    result = PlanResult("lp", "optimal" if status == pywraplp.Solver.OPTIMAL else "feasible",
                        elapsed, x=values, safety_stock=ss)
    result.binding = binding_constraints(problem, values)
    return result


def binding_constraints(problem: Problem, x: dict[tuple[int, int, int], float]) -> dict[tuple[str, str], list[str]]:
    """Plain-language list of what limited the plan, shown on recommendation cards."""
    out: dict[tuple[str, str], list[str]] = {}
    headroom = problem.headroom_now()
    lag = problem.lag
    dispatch_used = np.zeros(problem.D)
    for (r, f, j), v in x.items():
        if j == lag:
            dispatch_used[problem.route_src[r]] += v
    for s, sid in enumerate(problem.stations):
        routes_in = [r for r in range(problem.R) if problem.route_dst[r] == s]
        for f, fuel in enumerate(problem.fuels):
            tags: list[str] = []
            sent = sum(v for (r, ff, j), v in x.items() if j == lag and ff == f and problem.route_dst[r] == s)
            if sent > 0 and sent >= headroom[s, f] - 1:
                tags.append("tank_headroom")
            if problem.available_routes[s] <= 1:
                tags.append("single_route")
            if any(not problem.route_ok[r, lag] for r in routes_in):
                tags.append("route_disrupted")
            if any(dispatch_used[problem.route_src[r]] >= problem.dispatch_cap[problem.route_src[r], lag] - 1
                   for r in routes_in):
                tags.append("dispatch_capacity")
            if not problem.station_open[s, lag]:
                tags.append("station_outage")
            if tags:
                out[(sid, fuel)] = sorted(set(tags))
    return out
