"""Pydantic models for everything the simulator returns.

They follow the integration guide. Anything that doesn't parse, or breaks one
of the sanity rules below, is rejected so a bad response never reaches the
planner (brief section 11: "invalid simulator response -> reject input").
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Fuel = Literal["DIESEL", "PETROL", "OCTANE"]
FUELS: tuple[Fuel, ...] = ("DIESEL", "PETROL", "OCTANE")


class _Model(BaseModel):
    # the simulator may add fields in a later version; ignore them instead of failing
    model_config = ConfigDict(extra="ignore")


def _check_fuel_map(value: dict[str, float]) -> dict[str, float]:
    for fuel, liters in value.items():
        if fuel not in FUELS:
            raise ValueError(f"unknown fuel {fuel}")
        if liters < 0:
            raise ValueError(f"negative amount for {fuel}")
    return value


class Health(_Model):
    status: str
    database: str | None = None
    simulation: dict[str, Any] = Field(default_factory=dict)


class Instance(_Model):
    id: int
    scenario_id: str
    scenario_version: str | None = None
    seed: int | None = None
    sim_time: str
    tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    status: Literal["PAUSED", "RUNNING"]


class Region(_Model):
    id: str
    name: str
    demand_factor: float = Field(gt=0)


class Depot(_Model):
    id: str
    name: str
    region_id: str
    status: Literal["OPEN", "CONSTRAINED"]
    dispatch_capacity_per_tick: float = Field(ge=0)
    capacity: dict[str, float]
    inventory: dict[str, float]

    _fuels = field_validator("capacity", "inventory")(_check_fuel_map)


class Station(_Model):
    id: str
    name: str
    region_id: str
    status: Literal["OPEN", "OUTAGE"]
    demand_profile: str
    demand_multiplier: float = Field(gt=0)
    capacity: dict[str, float]
    inventory: dict[str, float]

    _fuels = field_validator("capacity", "inventory")(_check_fuel_map)


class Route(_Model):
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int = Field(ge=0)
    max_shipment: float = Field(gt=0)
    status: Literal["AVAILABLE", "DISRUPTED"]


class SupplyArrival(_Model):
    id: str
    depot_id: str
    fuel_type: Fuel
    quantity: float = Field(ge=0)
    planned_tick: int
    actual_tick: int | None = None
    status: Literal["SCHEDULED", "DELAYED", "ARRIVED"]


EventType = Literal[
    "demand_spike", "route_disruption", "station_outage",
    "depot_constraint", "shipment_delay", "supply_shortfall",
]


class Event(_Model):
    id: int
    type: EventType
    start_tick: int
    end_tick: int
    status: Literal["SCHEDULED", "ACTIVE", "RESOLVED"]
    parameters: dict[str, Any] = Field(default_factory=dict)


AllocationStatus = Literal["PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED"]


class Allocation(_Model):
    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: Fuel
    quantity: float = Field(gt=0)
    created_tick: int
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: AllocationStatus
    failure_reason: str | None = None


class DemandObs(_Model):
    id: int
    station_id: str
    fuel_type: Fuel
    tick: int
    sim_time: str | None = None
    demand_liters: float = Field(ge=0)
    served_liters: float = Field(ge=0)
    unmet_liters: float = Field(ge=0)


class Metrics(_Model):
    served_demand_liters: float = Field(ge=0)
    unmet_demand_liters: float = Field(ge=0)
    service_level: float = Field(ge=0, le=1)
    allocation_liters: float = Field(ge=0)
    allocation_failures: int = Field(ge=0)


class AllocationRequest(_Model):
    """Body for POST /v1/allocations. Validated before sending so a 422 never happens."""

    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: Fuel
    quantity: float = Field(gt=0)


# resource name -> (path, model, is_list)
RESOURCES: dict[str, tuple[str, type[_Model], bool]] = {
    "instance": ("/v1/instance", Instance, False),
    "regions": ("/v1/regions", Region, True),
    "depots": ("/v1/depots", Depot, True),
    "stations": ("/v1/stations", Station, True),
    "routes": ("/v1/routes", Route, True),
    "supply_arrivals": ("/v1/supply-arrivals", SupplyArrival, True),
    "events": ("/v1/events", Event, True),
    "allocations": ("/v1/allocations", Allocation, True),
    "demand_history": ("/v1/demand-history", DemandObs, True),
    "metrics": ("/v1/metrics", Metrics, False),
}
