"""Operating modes (design section 10).

Two separate signals decide the mode: API health, measured in real seconds,
and how old our station/depot data is, measured in ticks. A condition has to
hold for a while before we leave a worse mode, so the mode doesn't flap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent import metrics
from agent.sync.client import ApiHealth

MODES = ("NORMAL", "DEGRADED", "SAFE", "MANUAL")


@dataclass
class Transition:
    from_mode: str
    to_mode: str
    reason: str
    wall: float
    tick: int

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.from_mode, "to": self.to_mode, "reason": self.reason,
                "wall": self.wall, "tick": self.tick}


class ModeController:
    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        p = policy or {}
        self.error_ratio_degraded = float(p.get("error_ratio_degraded", 0.30))
        self.stale_degraded_s = float(p.get("stale_degraded_s", 3))
        self.no_read_safe_s = float(p.get("no_read_safe_s", 10))
        self.healthy_exit_s = float(p.get("healthy_exit_s", 5))
        self.age_degraded = int(p.get("age_degraded_ticks", 8))
        self.age_safe_max = int(p.get("age_safe_max_ticks", 24))
        self.age_ok = int(p.get("age_ok_ticks", 4))
        self.mode = "NORMAL"
        self.manual = False
        self.transitions: list[Transition] = []
        self._healthy_since: float | None = None
        self._publish()

    def a_safe(self, min_station_cover_ticks: float) -> int:
        return max(self.age_degraded + 1, min(self.age_safe_max, int(min_station_cover_ticks // 3)))

    def set_manual(self, on: bool, tick: int, who: str = "operator") -> Transition | None:
        self.manual = on
        if on and self.mode != "MANUAL":
            return self._move("MANUAL", f"set by {who}", tick)
        if not on and self.mode == "MANUAL":
            return self._move("NORMAL", f"released by {who}", tick)
        return None

    def update(self, health: ApiHealth, data_age: int, min_station_cover_ticks: float, tick: int,
               now: float | None = None) -> Transition | None:
        if self.manual:
            return None
        now = time.monotonic() if now is None else now
        error_ratio = health.error_ratio()
        since_success = health.seconds_since_success()
        stale_for = health.stale_for()
        a_safe = self.a_safe(min_station_cover_ticks)

        degraded_reasons = []
        if error_ratio > self.error_ratio_degraded:
            degraded_reasons.append(f"error ratio {error_ratio:.0%} over 10 s")
        if data_age > self.age_degraded:
            degraded_reasons.append(f"data is {data_age} ticks old")
        if stale_for > self.stale_degraded_s:
            degraded_reasons.append(f"stale data for {stale_for:.0f} s")
        safe_reasons = []
        if since_success > self.no_read_safe_s:
            safe_reasons.append(f"no successful read for {since_success:.0f} s")
        if data_age > a_safe:
            safe_reasons.append(f"data is {data_age} ticks old (limit {a_safe})")

        healthy = not degraded_reasons and not safe_reasons
        if healthy:
            if self._healthy_since is None:
                self._healthy_since = now
        else:
            self._healthy_since = None
        healthy_long_enough = self._healthy_since is not None and now - self._healthy_since >= self.healthy_exit_s

        if self.mode == "NORMAL" and (degraded_reasons or safe_reasons):
            return self._move("DEGRADED", "; ".join(degraded_reasons + safe_reasons), tick)
        if self.mode == "DEGRADED":
            if safe_reasons:
                return self._move("SAFE", "; ".join(safe_reasons), tick)
            if healthy_long_enough and data_age <= self.age_ok:
                return self._move("NORMAL", f"healthy for {self.healthy_exit_s:.0f} s", tick)
        if self.mode == "SAFE" and healthy_long_enough:
            return self._move("DEGRADED", f"healthy for {self.healthy_exit_s:.0f} s", tick)
        return None

    def _move(self, to_mode: str, reason: str, tick: int) -> Transition:
        transition = Transition(self.mode, to_mode, reason, time.time(), tick)
        self.mode = to_mode
        self._healthy_since = None
        self.transitions.append(transition)
        self.transitions = self.transitions[-200:]
        self._publish()
        return transition

    def _publish(self) -> None:
        for mode in MODES:
            metrics.MODE.labels(mode).set(1 if mode == self.mode else 0)
