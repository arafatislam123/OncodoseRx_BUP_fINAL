"""Confidence score for a recommendation (design section 11.1).

confidence = freshness x forecast quality x LP/heuristic agreement x model health
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Confidence:
    value: float
    freshness: float
    forecast: float
    agreement: float
    model_health: float

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


def freshness_score(age_ticks: int, stale: bool) -> float:
    score = 1.0 if age_ticks <= 1 else max(0.2, 1.0 - 0.06 * (age_ticks - 1))
    return score * (0.7 if stale else 1.0)


def forecast_score(mu: np.ndarray, sd: np.ndarray, coverage: float) -> float:
    mean = float(np.sum(mu))
    if mean <= 0:
        return 1.0
    width = 2 * 1.2816 * float(np.sqrt(np.sum(sd ** 2))) / mean   # (P90 - P10) / P50 over the lead time
    sharp = float(np.clip(1.2 - width, 0.3, 1.0))
    calibration = 1.0 - min(0.5, abs(coverage - 0.8) * 1.5)       # P10-P90 should hold ~80% of the time
    return sharp * calibration


def agreement_score(lp_liters: float, heuristic_liters: float, small: float = 500.0) -> float:
    if lp_liters <= small and heuristic_liters <= small:
        return 1.0
    low, high = sorted((lp_liters, heuristic_liters))
    if low <= small:
        return 0.6   # one policy wants to ship and the other doesn't
    return 0.6 + 0.4 * (low / high)


def model_health_score(mape: float) -> float:
    return float(np.clip(1.0 - mape, 0.4, 1.0))


def score(age_ticks: int, stale: bool, mu: np.ndarray, sd: np.ndarray, coverage: float,
          lp_liters: float, heuristic_liters: float, mape: float) -> Confidence:
    parts = (
        freshness_score(age_ticks, stale),
        forecast_score(mu, sd, coverage),
        agreement_score(lp_liters, heuristic_liters),
        model_health_score(mape),
    )
    return Confidence(float(np.prod(parts)), *parts)
