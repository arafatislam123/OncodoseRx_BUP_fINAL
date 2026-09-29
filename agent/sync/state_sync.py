"""Reads the simulator's REST API into the cache (design section 7.2).

REST is the source of truth. SSE only tells us when to call refresh() sooner.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

import structlog

from agent import metrics
from agent.sync.cache import WorldCache
from agent.sync.client import InvalidResponse, SimClient, SimError
from agent.sync.models import DemandObs
from agent.sync.snapshot import Snapshot

log = structlog.get_logger()

PARALLEL = ("stations", "depots", "routes", "events", "supply_arrivals")


class StateSync:
    def __init__(self, client: SimClient, cache: WorldCache, reconcile_interval_s: float = 2.0,
                 on_reset: Callable[[str], Awaitable[None]] | None = None,
                 on_invalid: Callable[[str, str], None] | None = None) -> None:
        self.client = client
        self.cache = cache
        self.reconcile_interval_s = reconcile_interval_s
        self.on_reset = on_reset
        self.on_invalid = on_invalid
        self.epoch = 0
        self._last_reconcile = 0.0
        self._last_metrics = 0.0
        self._force_reconcile = True
        self._seen_demand_ids: set[int] = set()
        self._last_demand_tick = -1
        self.new_demand: list[DemandObs] = []
        self.stale_age_ticks = 0  # how far behind stale data is, measured against SSE

    def request_reconcile(self) -> None:
        self._force_reconcile = True

    async def handle_reset(self, reason: str) -> None:
        """Called when the simulator was reset: allocations are wiped and every key is free again."""
        self.epoch += 1
        self.cache.clear()
        self._seen_demand_ids.clear()
        self._last_demand_tick = -1
        self._force_reconcile = True
        log.warning("simulator.reset_detected", reason=reason, epoch=self.epoch)
        if self.on_reset:
            await self.on_reset(reason)

    async def refresh(self, deadline: float | None = None) -> bool:
        """One sync pass. Returns True when station and depot data were read successfully."""
        ok_core = True
        instance_tick = await self._read_instance(deadline)

        tick = instance_tick if instance_tick is not None else self.cache.true_tick
        if self.cache.get("regions") is None:
            await self._read("regions", tick, deadline)

        results = await asyncio.gather(*(self._read(r, tick, deadline) for r in PARALLEL))
        for name, ok in zip(PARALLEL, results):
            if name in ("stations", "depots") and not ok:
                ok_core = False

        await self._read_demand(deadline)

        now = time.monotonic()
        if self._force_reconcile or now - self._last_reconcile >= self.reconcile_interval_s:
            if await self._read("allocations", tick, deadline):
                self._last_reconcile = now
                self._force_reconcile = False
        if now - self._last_metrics >= 1.0:
            if await self._read("metrics", tick, deadline, retries=0):
                self._last_metrics = now
                m = self.cache.get("metrics")
                metrics.SERVICE_LEVEL.set(m.service_level)
                metrics.UNMET_LITERS.set(m.unmet_demand_liters)

        self.cache.publish_ages()
        return ok_core

    async def _read_instance(self, deadline: float | None) -> int | None:
        try:
            result = await self.client.get("instance", deadline=deadline)
        except InvalidResponse as exc:
            self._invalid("instance", str(exc))
            return None
        except SimError:
            self.cache.fail("instance")
            return None

        inst = result.data
        if result.stale:
            # stale data describes the past; measure how far behind it is
            if self.cache.sse_tick >= 0:
                self.stale_age_ticks = max(0, self.cache.sse_tick - inst.tick)
            self.cache.put("instance", inst, inst.tick, stale=True)
            return None

        previous = self.cache.instance_tick
        if previous >= 0 and inst.tick < previous:
            await self.handle_reset(f"tick went backwards {previous} -> {inst.tick}")
        self.stale_age_ticks = 0
        self.cache.instance_tick = inst.tick
        self.cache.put("instance", inst, inst.tick, stale=False)
        return inst.tick

    async def _read(self, resource: str, tick: int, deadline: float | None, retries: int = 2) -> bool:
        try:
            result = await self.client.get(resource, retries=retries, deadline=deadline)
        except InvalidResponse as exc:
            self._invalid(resource, str(exc))
            return False
        except SimError:
            self.cache.fail(resource)
            return False
        self.cache.put(resource, result.data, tick, stale=result.stale)
        return not result.stale

    async def _read_demand(self, deadline: float | None) -> None:
        tick = self.cache.true_tick
        since = tick - self._last_demand_tick if self._last_demand_tick >= 0 else 100
        limit = max(1, min(2000, 12 * (since + 2)))
        try:
            result = await self.client.get("demand_history", params={"limit": limit}, deadline=deadline)
        except InvalidResponse as exc:
            self._invalid("demand_history", str(exc))
            return
        except SimError:
            return
        if result.stale:
            return
        fresh = [row for row in result.data if row.id not in self._seen_demand_ids]
        fresh.sort(key=lambda row: (row.tick, row.id))
        for row in fresh:
            self._seen_demand_ids.add(row.id)
            self._last_demand_tick = max(self._last_demand_tick, row.tick)
        self.new_demand.extend(fresh)
        # keep the id set from growing forever; ids only increase
        if len(self._seen_demand_ids) > 50_000:
            cutoff = sorted(self._seen_demand_ids)[-20_000]
            self._seen_demand_ids = {i for i in self._seen_demand_ids if i >= cutoff}

    def take_new_demand(self) -> list[DemandObs]:
        rows, self.new_demand = self.new_demand, []
        return rows

    def _invalid(self, resource: str, message: str) -> None:
        log.warning("response.invalid", endpoint=resource, error=message[:300])
        self.cache.fail(resource)
        if self.on_invalid:
            self.on_invalid(resource, message)

    def snapshot(self) -> Snapshot | None:
        c = self.cache
        stations, depots, routes = c.get("stations"), c.get("depots"), c.get("routes")
        instance = c.get("instance")
        if not (stations and depots and routes and instance):
            return None
        return Snapshot(
            tick=c.true_tick,
            data_tick=min(c.entries["stations"].fetched_tick, c.entries["depots"].fetched_tick),
            status=instance.status,
            tick_minutes=instance.tick_minutes,
            stations={s.id: s for s in stations},
            depots={d.id: d for d in depots},
            routes={r.id: r for r in routes},
            regions={r.id: r for r in (c.get("regions") or [])},
            events=list(c.get("events") or []),
            arrivals=list(c.get("supply_arrivals") or []),
            allocations=list(c.get("allocations") or []),
            ages={name: c.age(name) for name in ("stations", "depots", "routes", "events",
                                                   "supply_arrivals", "allocations")},
            stale=c.any_stale(),
            epoch=self.epoch,
        )
