"""Structural demand prior from config/world_prior.yaml (integration guide section 8)."""

from __future__ import annotations

from typing import Any

FUELS = ("DIESEL", "PETROL", "OCTANE")


class WorldModel:
    def __init__(self, prior: dict[str, Any], tick_minutes: int = 15) -> None:
        self.prior = prior
        self.tick_minutes = tick_minutes
        self.ticks_per_day = int(24 * 60 / tick_minutes)
        self.start_hour = float(prior.get("sim_start_hour", 0))
        self.baseline_multiplier = float(prior.get("baseline_station_multiplier", 1.0))
        self._profiles: dict[str, dict[str, float]] = prior.get("profiles", {})
        self._hours: dict[str, dict[str, Any]] = prior.get("hour_factors", {})
        self._regions: dict[str, dict[str, float]] = prior.get("regions", {})

    def hour_of(self, tick: int) -> float:
        return (self.start_hour + tick * self.tick_minutes / 60.0) % 24.0

    def is_busy(self, profile: str, tick: int) -> bool:
        spec = self._hours.get(profile)
        if not spec:
            return False
        hour = self.hour_of(tick)
        return any(start <= hour < end for start, end in spec.get("busy", []))

    def hour_factor(self, profile: str, tick: int) -> float:
        spec = self._hours.get(profile)
        if not spec:
            return 1.0
        return float(spec["busy_factor"] if self.is_busy(profile, tick) else spec["off_factor"])

    def bucket(self, profile: str, tick: int) -> int:
        """Four buckets per profile: busy/quiet x first/second half of the day."""
        busy = self.is_busy(profile, tick)
        late = self.hour_of(tick) >= 12
        return (0 if busy else 2) + (1 if late else 0)

    def daily(self, profile: str, fuel: str) -> float:
        return float(self._profiles.get(profile, {}).get(fuel, 0.0))

    def noise(self, profile: str) -> float:
        return float(self._profiles.get(profile, {}).get("noise", 0.1))

    def region_factor(self, region_id: str, live_factor: float | None = None) -> float:
        if live_factor is not None:
            return live_factor
        return float(self._regions.get(region_id, {}).get("demand_factor", 1.0))

    def prior_mean(self, profile: str, fuel: str, region_factor: float, tick: int) -> float:
        """Expected liters in one tick, before event multipliers and learned corrections."""
        per_tick = self.daily(profile, fuel) / self.ticks_per_day
        return per_tick * region_factor * self.hour_factor(profile, tick)
