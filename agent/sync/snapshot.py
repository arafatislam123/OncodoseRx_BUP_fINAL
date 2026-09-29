"""A consistent view of the world handed to the intelligence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.sync.models import (
    Allocation, Depot, Event, Region, Route, Station, SupplyArrival,
)


@dataclass
class Snapshot:
    tick: int                      # best estimate of the real tick
    data_tick: int                 # tick the station/depot data describes
    status: str                    # PAUSED | RUNNING
    tick_minutes: int
    stations: dict[str, Station]
    depots: dict[str, Depot]
    routes: dict[str, Route]
    regions: dict[str, Region]
    events: list[Event]
    arrivals: list[SupplyArrival]
    allocations: list[Allocation]
    ages: dict[str, int] = field(default_factory=dict)
    stale: bool = False
    epoch: int = 0

    # --- helpers used all over the intelligence layer -------------------
    def routes_to(self, station_id: str, only_available: bool = False) -> list[Route]:
        return [
            r for r in self.routes.values()
            if r.destination_station_id == station_id
            and (not only_available or r.status == "AVAILABLE")
        ]

    def routes_from(self, depot_id: str) -> list[Route]:
        return [r for r in self.routes.values() if r.source_depot_id == depot_id]

    def is_cross_region(self, route: Route) -> bool:
        depot = self.depots.get(route.source_depot_id)
        station = self.stations.get(route.destination_station_id)
        return bool(depot and station and depot.region_id != station.region_id)

    def open_allocations(self) -> list[Allocation]:
        return [a for a in self.allocations if a.status in ("PENDING", "IN_TRANSIT")]

    def max_age(self) -> int:
        return max((self.ages.get(k, 0) for k in ("stations", "depots")), default=0)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly copy, stored with decision records so any decision can be replayed."""
        return {
            "tick": self.tick,
            "data_tick": self.data_tick,
            "status": self.status,
            "tick_minutes": self.tick_minutes,
            "epoch": self.epoch,
            "stale": self.stale,
            "ages": self.ages,
            "stations": [s.model_dump() for s in self.stations.values()],
            "depots": [d.model_dump() for d in self.depots.values()],
            "routes": [r.model_dump() for r in self.routes.values()],
            "regions": [r.model_dump() for r in self.regions.values()],
            "events": [e.model_dump() for e in self.events],
            "arrivals": [a.model_dump() for a in self.arrivals],
            "allocations": [a.model_dump() for a in self.open_allocations()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            tick=data["tick"],
            data_tick=data.get("data_tick", data["tick"]),
            status=data.get("status", "PAUSED"),
            tick_minutes=data.get("tick_minutes", 15),
            stations={s["id"]: Station.model_validate(s) for s in data["stations"]},
            depots={d["id"]: Depot.model_validate(d) for d in data["depots"]},
            routes={r["id"]: Route.model_validate(r) for r in data["routes"]},
            regions={r["id"]: Region.model_validate(r) for r in data.get("regions", [])},
            events=[Event.model_validate(e) for e in data.get("events", [])],
            arrivals=[SupplyArrival.model_validate(a) for a in data.get("arrivals", [])],
            allocations=[Allocation.model_validate(a) for a in data.get("allocations", [])],
            ages=data.get("ages", {}),
            stale=data.get("stale", False),
            epoch=data.get("epoch", 0),
        )
