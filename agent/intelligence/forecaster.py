"""Demand forecaster (design section 8.2).

mean(s, f, k) = prior(profile, hour, region) x M(s, k) x c(s, f, bucket)

c is a learned correction kept as a normal posterior on log(c). It starts at
1.0, learns quickly while there is little data, and keeps a small amount of
process noise so it can still follow drift. A change-point reset widens the
posterior again when CUSUM finds a shift no known event explains.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from agent import metrics
from agent.intelligence.multipliers import MultiplierModel
from agent.intelligence.world import FUELS, WorldModel
from agent.sync.models import DemandObs, Event, Station
from agent.sync.snapshot import Snapshot

Z90 = 1.2816


@dataclass
class Posterior:
    m: float = 0.0     # mean of log(correction)
    v: float = 0.04    # variance of log(correction)
    n: int = 0


@dataclass
class ResidualStats:
    rel_var: float = 0.0    # EWMA of squared relative error
    ape: float = 0.0        # EWMA of absolute percentage error
    inside: float = 0.8     # EWMA of "observation fell inside P10-P90"
    n: int = 0


@dataclass
class Observation:
    station: str
    fuel: str
    tick: int
    demand: float
    mean: float
    sigma: float
    z: float
    censored: bool


class Forecaster:
    def __init__(self, world: WorldModel, policy: dict[str, Any] | None = None,
                 censoring_rule: str = "exclude_stockout") -> None:
        policy = policy or {}
        self.world = world
        self.prior_var = float(policy.get("prior_var", 0.04))
        self.process_var = float(policy.get("process_var", 0.0005))
        self.min_obs_var = float(policy.get("min_obs_var", 0.004))
        self.resid_alpha = float(policy.get("resid_ewma", 0.05))
        self.censoring_rule = censoring_rule
        self.posteriors: dict[tuple[str, str, int], Posterior] = {}
        self.residuals: dict[tuple[str, str], ResidualStats] = {}
        self.healthy = True

    # --- prediction -----------------------------------------------------
    def _post(self, station: str, fuel: str, bucket: int) -> Posterior:
        key = (station, fuel, bucket)
        if key not in self.posteriors:
            self.posteriors[key] = Posterior(v=self.prior_var)
        return self.posteriors[key]

    def prior_mean(self, station: Station, fuel: str, tick: int, region_factor: float) -> float:
        return self.world.prior_mean(station.demand_profile, fuel, region_factor, tick)

    def mean(self, station: Station, fuel: str, tick: int, region_factor: float, mult: float) -> float:
        post = self._post(station.id, fuel, self.world.bucket(station.demand_profile, tick))
        return self.prior_mean(station, fuel, tick, region_factor) * mult * math.exp(post.m)

    def sigma(self, station: Station, fuel: str, mean: float) -> float:
        rel = self.world.noise(station.demand_profile)
        stats = self.residuals.get((station.id, fuel))
        if stats and stats.n >= 20:
            rel = max(rel, math.sqrt(stats.rel_var))
        return max(1e-6, rel * mean)

    def forecast(self, snapshot: Snapshot, mm: MultiplierModel, station_id: str, fuel: str,
                 start_tick: int, horizon: int) -> tuple[np.ndarray, np.ndarray]:
        """Mean and sigma for ticks start_tick .. start_tick + horizon - 1."""
        station = snapshot.stations[station_id]
        rf = self._region_factor(snapshot, station)
        mu = np.empty(horizon)
        sd = np.empty(horizon)
        for i in range(horizon):
            tick = start_tick + i
            mu[i] = self.mean(station, fuel, tick, rf, mm.m(station_id, tick))
            sd[i] = self.sigma(station, fuel, mu[i])
        if not np.all(np.isfinite(mu)):
            self.healthy = False
            raise FloatingPointError("forecast produced non-finite values")
        return mu, sd

    def forecast_all(self, snapshot: Snapshot, mm: MultiplierModel, start_tick: int,
                     horizon: int) -> dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]:
        return {
            (sid, fuel): self.forecast(snapshot, mm, sid, fuel, start_tick, horizon)
            for sid in snapshot.stations for fuel in FUELS
        }

    @staticmethod
    def quantiles(mu: np.ndarray, sd: np.ndarray) -> dict[str, np.ndarray]:
        return {"p10": np.maximum(0, mu - Z90 * sd), "p50": mu, "p90": mu + Z90 * sd}

    @staticmethod
    def lead_time_sigma(sd: np.ndarray) -> float:
        """Sum variances tick by tick across the real hourly profile, not noise x mean x sqrt(L)."""
        return float(math.sqrt(float(np.sum(sd ** 2))))

    def _region_factor(self, snapshot: Snapshot, station: Station) -> float:
        region = snapshot.regions.get(station.region_id)
        return self.world.region_factor(station.region_id, region.demand_factor if region else None)

    # --- learning -------------------------------------------------------
    def is_censored(self, row: DemandObs, station: Station, mm: MultiplierModel,
                    events: Iterable[Event]) -> bool:
        if row.unmet_liters > 0 and self.censoring_rule != "include":
            return True  # a stockout hides the real demand
        if mm.near_transition(station.id, row.tick):
            return True
        for event in events:
            if event.type != "station_outage" or not (event.start_tick <= row.tick < event.end_tick):
                continue
            ids = (event.parameters or {}).get("station_ids") or []
            if not ids or station.id in ids:
                return True
        return False

    def observe(self, rows: list[DemandObs], snapshot: Snapshot, mm: MultiplierModel) -> list[Observation]:
        """Learn from new demand rows. Returns standardised residuals for the detector."""
        out: list[Observation] = []
        for row in rows:
            station = snapshot.stations.get(row.station_id)
            if station is None:
                continue
            rf = self._region_factor(snapshot, station)
            mult = mm.m(station.id, row.tick)
            mean = self.mean(station, row.fuel_type, row.tick, rf, mult)
            sd = self.sigma(station, row.fuel_type, mean)
            censored = self.is_censored(row, station, mm, snapshot.events)
            z = (row.demand_liters - mean) / sd if sd > 0 else 0.0
            out.append(Observation(station.id, row.fuel_type, row.tick, row.demand_liters, mean, sd, z, censored))
            if censored or mean <= 0:
                continue
            self._update_stats(station.id, row.fuel_type, row.demand_liters, mean, sd)
            if row.demand_liters > 0:
                prior = self.prior_mean(station, row.fuel_type, row.tick, rf) * mult
                self._update_posterior(station, row.fuel_type, row.tick, row.demand_liters, prior)
        self._publish()
        return out

    def _update_posterior(self, station: Station, fuel: str, tick: int, demand: float, prior: float) -> None:
        post = self._post(station.id, fuel, self.world.bucket(station.demand_profile, tick))
        noise = self.world.noise(station.demand_profile)
        obs_var = max(self.min_obs_var, math.log(1 + noise ** 2))
        x = math.log(demand / prior)
        post.v += self.process_var
        new_v = 1.0 / (1.0 / post.v + 1.0 / obs_var)
        post.m = new_v * (post.m / post.v + x / obs_var)
        post.v = new_v
        post.n += 1

    def _update_stats(self, station: str, fuel: str, demand: float, mean: float, sd: float) -> None:
        stats = self.residuals.setdefault((station, fuel), ResidualStats())
        a = self.resid_alpha if stats.n >= 20 else 1.0 / (stats.n + 1)
        rel = (demand - mean) / mean
        stats.rel_var = (1 - a) * stats.rel_var + a * rel * rel
        stats.ape = (1 - a) * stats.ape + a * abs(rel)
        inside = 1.0 if mean - Z90 * sd <= demand <= mean + Z90 * sd else 0.0
        stats.inside = (1 - a) * stats.inside + a * inside
        stats.n += 1

    def reset_changepoint(self, station: str, fuel: str) -> None:
        for (s, f, _), post in self.posteriors.items():
            if s == station and f == fuel:
                post.v = max(post.v, self.prior_var)

    def coverage(self) -> float:
        values = [s.inside for s in self.residuals.values() if s.n >= 20]
        return float(np.mean(values)) if values else 0.8

    def mape(self, station: str, fuel: str) -> float:
        stats = self.residuals.get((station, fuel))
        return stats.ape if stats else 0.0

    def _publish(self) -> None:
        for (station, fuel), stats in self.residuals.items():
            metrics.FORECAST_APE.labels(station, fuel).set(stats.ape)
        metrics.FORECAST_COVERAGE.set(self.coverage())

    # --- persistence ----------------------------------------------------
    def dump_state(self) -> list[tuple[str, str, int, float, float, int]]:
        return [(s, f, b, p.m, p.v, p.n) for (s, f, b), p in self.posteriors.items()]

    def load_state(self, rows: Iterable[tuple[str, str, int, float, float, int]]) -> None:
        for s, f, b, m, v, n in rows:
            self.posteriors[(s, f, int(b))] = Posterior(m=float(m), v=float(v), n=int(n))
