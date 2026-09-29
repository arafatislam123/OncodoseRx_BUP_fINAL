"""Per-resource cache with freshness tracking (design section 7.1)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent import metrics

NEVER = 10**6  # age used for data we have never seen fresh


@dataclass
class CacheEntry:
    data: Any = None
    fetched_wall: float = 0.0
    fetched_tick: int = -1
    last_fresh_wall: float = 0.0
    last_fresh_tick: int = -1
    stale: bool = False
    stale_data: Any = None  # kept on the side, never used to overwrite fresher data
    consecutive_failures: int = 0


class WorldCache:
    RESOURCES = (
        "instance", "regions", "stations", "depots", "routes",
        "events", "supply_arrivals", "allocations", "metrics",
    )

    def __init__(self) -> None:
        self.entries: dict[str, CacheEntry] = {name: CacheEntry() for name in self.RESOURCES}
        self.sse_tick: int = -1        # latest simulation.tick seen on SSE
        self.instance_tick: int = -1   # latest tick from a fresh /v1/instance read

    @property
    def true_tick(self) -> int:
        """Best guess of the simulator's real tick. SSE isn't affected by the stale fault."""
        return max(self.sse_tick, self.instance_tick)

    def get(self, resource: str) -> Any:
        return self.entries[resource].data

    def put(self, resource: str, data: Any, tick: int, stale: bool) -> None:
        entry = self.entries[resource]
        now = time.monotonic()
        entry.consecutive_failures = 0
        entry.stale = stale
        if stale and entry.data is not None:
            entry.stale_data = data
            return
        entry.data = data
        entry.fetched_wall = now
        entry.fetched_tick = tick
        if not stale:
            entry.last_fresh_wall = now
            entry.last_fresh_tick = tick

    def fail(self, resource: str) -> None:
        self.entries[resource].consecutive_failures += 1

    def age(self, resource: str) -> int:
        entry = self.entries[resource]
        if entry.last_fresh_tick < 0:
            return NEVER
        return max(0, self.true_tick - entry.last_fresh_tick)

    def any_stale(self) -> bool:
        return any(entry.stale for entry in self.entries.values())

    def publish_ages(self) -> None:
        for name in ("stations", "depots", "routes", "events", "supply_arrivals", "allocations"):
            metrics.DATA_AGE.labels(name).set(min(self.age(name), 9999))

    def clear(self) -> None:
        self.entries = {name: CacheEntry() for name in self.RESOURCES}
        self.sse_tick = -1
        self.instance_tick = -1
