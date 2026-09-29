"""Monte Carlo projection engine, the "Simulate" step (design section 8.5).

It runs the same station state updates as the planner, but with sampled
demand, so we can compare "do nothing" against a plan and put a number on the
risk: "stockout risk 72% -> 19%".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from agent.intelligence.problem import Problem


@dataclass
class ProjectionResult:
    p_stockout: float
    time_to_stockout_ticks: int | None
    expected_unmet: float
    end_p10: float
    end_p50: float
    end_p90: float
    band_p10: list[float]
    band_p90: list[float]

    def to_dict(self) -> dict:
        return {
            "p_stockout": round(self.p_stockout, 3),
            "time_to_stockout_ticks": self.time_to_stockout_ticks,
            "expected_unmet": round(self.expected_unmet, 1),
            "end_p10": round(self.end_p10), "end_p50": round(self.end_p50), "end_p90": round(self.end_p90),
        }


def plan_arrivals(problem: Problem, orders: dict[tuple[int, int, int], float]) -> np.ndarray:
    """Convert {(route, fuel, slot): liters} into arrivals [S, F, H]."""
    arrivals = np.zeros((problem.S, problem.F, problem.H))
    for (r, f, slot), liters in orders.items():
        k = problem.arrival_index(r, slot)
        if 0 <= k < problem.H and liters > 0:
            arrivals[problem.route_dst[r], f, k] += liters
    return arrivals


def project(problem: Problem, extra: np.ndarray | None = None, samples: int = 200,
            seed: int = 7, horizon: int | None = None, overflow: str = "lost",
            bands: bool = False) -> dict[tuple[str, str], ProjectionResult]:
    """Project every station/fuel forward. extra holds arrivals from a proposed plan."""
    H = horizon or problem.H
    S, F = problem.S, problem.F
    rng = np.random.default_rng(seed)
    mu = problem.mu[:, :, :H]
    rel = np.divide(problem.sd[:, :, :H], mu, out=np.zeros_like(mu), where=mu > 0)
    eps = rng.standard_normal((samples, S, F, H))
    demand = mu[None] * np.exp(rel[None] * eps - 0.5 * rel[None] ** 2)

    arrivals = problem.intransit[:, :, :H].copy()
    if extra is not None:
        arrivals = arrivals + extra[:, :, :H]

    inv = np.broadcast_to(problem.inv0, (samples, S, F)).copy()
    if np.any(problem.inv0_sd > 0):
        inv = np.clip(inv + rng.standard_normal((samples, S, F)) * problem.inv0_sd, 0, problem.cap)

    first_out = np.full((samples, S, F), -1)
    unmet_total = np.zeros((samples, S, F))
    path = np.zeros((samples, S, F, H))
    for k in range(H):
        level = inv + arrivals[None, :, :, k]
        if overflow != "kept":
            level = np.minimum(level, problem.cap[None])
        served = np.minimum(level, demand[:, :, :, k])
        short = demand[:, :, :, k] - served
        unmet_total += short
        newly = (short > 0.5) & (first_out < 0)
        first_out[newly] = k
        inv = level - served
        path[:, :, :, k] = inv

    hit_all = first_out >= 0
    p_out = hit_all.mean(axis=0)
    unmet_mean = unmet_total.mean(axis=0)
    end_q = np.percentile(path[:, :, :, H - 1], [10, 50, 90], axis=0)
    band = np.percentile(path, [10, 90], axis=0).round(1) if bands else None

    results: dict[tuple[str, str], ProjectionResult] = {}
    for s, sid in enumerate(problem.stations):
        for f, fuel in enumerate(problem.fuels):
            outs = first_out[:, s, f]
            hit = hit_all[:, s, f]
            ttso = int(np.median(outs[hit])) + 1 if hit.any() else None
            results[(sid, fuel)] = ProjectionResult(
                p_stockout=float(p_out[s, f]),
                time_to_stockout_ticks=ttso,
                expected_unmet=float(unmet_mean[s, f]),
                end_p10=float(end_q[0, s, f]),
                end_p50=float(end_q[1, s, f]),
                end_p90=float(end_q[2, s, f]),
                band_p10=band[0, s, f].tolist() if band is not None else [],
                band_p90=band[1, s, f].tolist() if band is not None else [],
            )
    return results
