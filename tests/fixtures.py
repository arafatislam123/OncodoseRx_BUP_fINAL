"""The baseline world from section 8 of the integration guide, as simulator payloads.

Used by the unit tests and by the fake simulator in tests/fakesim.py.
"""

from __future__ import annotations

import copy
from typing import Any

REGIONS = [
    {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.00},
    {"id": "region-chattogram", "name": "Chattogram Division", "demand_factor": 1.08},
]

DEPOTS = [
    {"id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN",
     "dispatch_capacity_per_tick": 12000,
     "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
     "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000}},
    {"id": "depot-patiya", "name": "Patiya Depot", "region_id": "region-chattogram", "status": "OPEN",
     "dispatch_capacity_per_tick": 11000,
     "capacity": {"DIESEL": 85000, "PETROL": 65000, "OCTANE": 40000},
     "inventory": {"DIESEL": 55000, "PETROL": 42000, "OCTANE": 24000}},
]

STATIONS = [
    {"id": "station-mirpur", "name": "Mirpur Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
     "demand_profile": "urban_high", "demand_multiplier": 1.0,
     "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
     "inventory": {"DIESEL": 9000, "PETROL": 9000, "OCTANE": 5000}},
    {"id": "station-tongi", "name": "Tongi Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
     "demand_profile": "industrial", "demand_multiplier": 1.0,
     "capacity": {"DIESEL": 18000, "PETROL": 9000, "OCTANE": 6000},
     "inventory": {"DIESEL": 11000, "PETROL": 6000, "OCTANE": 3500}},
    {"id": "station-karnaphuli", "name": "Karnaphuli Fuel Station", "region_id": "region-chattogram",
     "status": "OPEN", "demand_profile": "highway", "demand_multiplier": 1.0,
     "capacity": {"DIESEL": 14000, "PETROL": 15000, "OCTANE": 9000},
     "inventory": {"DIESEL": 8500, "PETROL": 9500, "OCTANE": 5200}},
    {"id": "station-coxsbazar", "name": "Cox's Bazar Fuel Station", "region_id": "region-chattogram",
     "status": "OPEN", "demand_profile": "regional", "demand_multiplier": 1.0,
     "capacity": {"DIESEL": 12000, "PETROL": 12000, "OCTANE": 7000},
     "inventory": {"DIESEL": 7500, "PETROL": 7500, "OCTANE": 4200}},
]

ROUTES = [
    {"id": "route-gazipur-mirpur", "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
     "transit_ticks": 2, "max_shipment": 7000, "status": "AVAILABLE"},
    {"id": "route-gazipur-tongi", "source_depot_id": "depot-gazipur", "destination_station_id": "station-tongi",
     "transit_ticks": 2, "max_shipment": 6500, "status": "AVAILABLE"},
    {"id": "route-patiya-karnaphuli", "source_depot_id": "depot-patiya",
     "destination_station_id": "station-karnaphuli", "transit_ticks": 2, "max_shipment": 7000,
     "status": "AVAILABLE"},
    {"id": "route-patiya-coxsbazar", "source_depot_id": "depot-patiya", "destination_station_id": "station-coxsbazar",
     "transit_ticks": 3, "max_shipment": 6000, "status": "AVAILABLE"},
    {"id": "route-gazipur-karnaphuli", "source_depot_id": "depot-gazipur",
     "destination_station_id": "station-karnaphuli", "transit_ticks": 4, "max_shipment": 5000,
     "status": "AVAILABLE"},
    {"id": "route-patiya-mirpur", "source_depot_id": "depot-patiya", "destination_station_id": "station-mirpur",
     "transit_ticks": 4, "max_shipment": 5000, "status": "AVAILABLE"},
]


def supply_schedule() -> list[dict[str, Any]]:
    """4 initial arrivals at ticks 12-20, then 18 top-ups every 64 ticks (guide 8.7)."""
    out: list[dict[str, Any]] = []
    initial = [("depot-gazipur", "DIESEL", 18000, 12), ("depot-patiya", "DIESEL", 16000, 14),
               ("depot-gazipur", "PETROL", 14000, 17), ("depot-patiya", "PETROL", 13000, 20)]
    for i, (depot, fuel, qty, tick) in enumerate(initial, start=1):
        out.append({"id": f"supply-{i:03d}", "depot_id": depot, "fuel_type": fuel, "quantity": qty,
                    "planned_tick": tick, "actual_tick": None, "status": "SCHEDULED"})
    daily = {"depot-gazipur": {"DIESEL": 22500, "PETROL": 15000, "OCTANE": 7800},
             "depot-patiya": {"DIESEL": 19100, "PETROL": 20000, "OCTANE": 10600}}
    combos = [(d, f) for d in daily for f in ("DIESEL", "PETROL", "OCTANE")]
    for n in range(18):
        depot, fuel = combos[n % len(combos)]
        out.append({"id": f"supply-{n + 5:03d}", "depot_id": depot, "fuel_type": fuel,
                    "quantity": daily[depot][fuel] * 3, "planned_tick": 64 * (n // len(combos) + 1) + n % 6,
                    "actual_tick": None, "status": "SCHEDULED"})
    return out


def payloads() -> dict[str, Any]:
    return copy.deepcopy({
        "regions": REGIONS, "depots": DEPOTS, "stations": STATIONS, "routes": ROUTES,
        "supply_arrivals": supply_schedule(),
    })


def snapshot(tick: int = 0, **overrides: Any):
    """A Snapshot of the baseline world at a given tick."""
    from agent.sync.snapshot import Snapshot

    data = payloads()
    data.update(overrides)
    return Snapshot.from_dict({
        "tick": tick, "data_tick": tick, "status": "RUNNING", "tick_minutes": 15,
        "stations": data["stations"], "depots": data["depots"], "routes": data["routes"],
        "regions": data["regions"], "events": data.get("events", []),
        "arrivals": data["supply_arrivals"], "allocations": data.get("allocations", []),
    })
