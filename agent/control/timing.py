"""Tick length and landing lag (design section 6.1).

The planner plans for the tick an order will actually land on, not the tick
the snapshot was taken on. Under a 500 ms latency fault at speed 8 that's 4-5
ticks later.
"""

from __future__ import annotations

import math
import time


class Timing:
    def __init__(self, simulation_speed: float = 8.0, stepped: bool = False) -> None:
        self.tick_period_s = 1.0 / simulation_speed if simulation_speed > 0 else math.inf
        self.stepped = stepped
        self.running = True
        self.cycle_time_s = 0.05
        self.correction = 0.0          # learned from created_tick vs the tick we planned for
        self._last_tick: int | None = None
        self._last_wall: float | None = None

    def observe_tick(self, tick: int, wall: float | None = None) -> None:
        wall = time.monotonic() if wall is None else wall
        if self._last_tick is not None and self._last_wall is not None and tick > self._last_tick:
            per_tick = (wall - self._last_wall) / (tick - self._last_tick)
            if 0 < per_tick < 30:
                self.tick_period_s = 0.8 * self.tick_period_s + 0.2 * per_tick
        self._last_tick, self._last_wall = tick, wall

    def observe_cycle(self, seconds: float) -> None:
        self.cycle_time_s = 0.8 * self.cycle_time_s + 0.2 * seconds

    def observe_created(self, planned_tick: int, created_tick: int) -> None:
        error = created_tick - planned_tick
        self.correction = max(-2.0, min(8.0, 0.8 * self.correction + 0.2 * error))

    def land_lag(self, post_latency_p90_s: float) -> int:
        if self.stepped or not self.running or not math.isfinite(self.tick_period_s):
            return 0
        raw = (self.cycle_time_s + post_latency_p90_s) / self.tick_period_s
        return max(0, min(12, int(math.ceil(raw + self.correction - 1e-9)) - 1))
