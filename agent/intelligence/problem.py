"""Turns a snapshot plus forecasts into arrays the planners and the projection engine share.

Time indexing used everywhere in planning:

* slot j (0..H-1) means "an order created at tick t0 + j"
* index k (0..H-1) means "what happens during tick t0 + 1 + k"

An order created at t0 + j departs at t0 + j + departure_delay and lands at
t0 + j + departure_delay + transit, which is index j + departure_delay + transit - 1.
That matches the guide's example: created 5, departs 6, arrives 8 on a 2-tick route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from agent.intelligence.forecaster import Forecaster
from agent.intelligence.multipliers import MultiplierModel
from agent.intelligence.world import FUELS
from agent.sync.models import Event, Route
from agent.sync.snapshot import Snapshot


@dataclass
class InFlight:
    """An order we may have sent but haven't confirmed (an UNKNOWN intent)."""

    depot: str
    station: str
    route: str
    fuel: str
    quantity: float
    created_tick: int


@dataclass
class Problem:
    t0: int
    lag: int
    H: int
    dd: int
    stations: list[str]
    depots: list[str]
    routes: list[Route]
    fuels: tuple[str, ...]
    inv0: np.ndarray             # [S, F] station inventory now (nowcast)
    inv0_sd: np.ndarray          # [S, F] extra uncertainty from stale data
    cap: np.ndarray              # [S, F]
    depot_inv0: np.ndarray       # [D, F]
    supply: np.ndarray           # [D, F, H] depot supply landing at tick t0 + j
    intransit: np.ndarray        # [S, F, H] existing shipments landing at index k
    mu: np.ndarray               # [S, F, H]
    sd: np.ndarray               # [S, F, H]
    route_ok: np.ndarray         # [R, H] can an order be created on route r at slot j
    station_open: np.ndarray     # [S, H] station open at creation slot j
    demand_on: np.ndarray        # [S, H] 0 while a station is in outage (index k)
    dispatch_cap: np.ndarray     # [D, H]
    route_src: np.ndarray        # [R] depot index
    route_dst: np.ndarray        # [R] station index
    route_transit: np.ndarray    # [R]
    route_max: np.ndarray        # [R]
    cross: np.ndarray            # [R] bool
    available_routes: np.ndarray  # [S] count of routes usable now
    in_transit_total: np.ndarray  # [S, F] everything already on its way
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def S(self) -> int:
        return len(self.stations)

    @property
    def F(self) -> int:
        return len(self.fuels)

    @property
    def D(self) -> int:
        return len(self.depots)

    @property
    def R(self) -> int:
        return len(self.routes)

    def arrival_index(self, r: int, slot: int) -> int:
        return slot + self.dd + int(self.route_transit[r]) - 1

    def headroom_now(self, count_in_transit: bool = True) -> np.ndarray:
        used = self.inv0 + (self.in_transit_total if count_in_transit else 0)
        return np.maximum(0.0, self.cap - used)


def _disrupted_windows(events: Iterable[Event], etype: str, key: str, entity: str,
                       now: int, currently_down: bool) -> list[tuple[int, int]]:
    """Tick windows [start, end) in which an entity is down, from events and current status."""
    windows: list[tuple[int, int]] = []
    covered_now = False
    for event in events:
        if event.type != etype or event.status == "RESOLVED":
            continue
        ids = (event.parameters or {}).get(key) or []
        if ids and entity not in ids:
            continue
        windows.append((event.start_tick, event.end_tick))
        if event.start_tick <= now < event.end_tick:
            covered_now = True
    if currently_down and not covered_now:
        # down with no event we can see: assume it stays down for the whole horizon
        windows.append((now, now + 10**6))
    return windows


def _in_windows(tick: int, windows: list[tuple[int, int]]) -> bool:
    return any(start <= tick < end for start, end in windows)


def build_problem(snapshot: Snapshot, forecaster: Forecaster, mm: MultiplierModel, *,
                  horizon: int, lag: int, facts: dict[str, Any] | None = None,
                  in_flight: list[InFlight] | None = None,
                  shortfall_ratio: dict[str, float] | None = None) -> Problem:
    facts = facts or {}
    dd = int(facts.get("departure_delay_ticks", 1))
    derate = float(facts.get("constrained_derate", 0.5))
    t0 = snapshot.tick
    H = horizon
    stations = sorted(snapshot.stations)
    depots = sorted(snapshot.depots)
    routes = sorted(snapshot.routes.values(), key=lambda r: r.id)
    s_idx = {s: i for i, s in enumerate(stations)}
    d_idx = {d: i for i, d in enumerate(depots)}
    S, D, R, F = len(stations), len(depots), len(routes), len(FUELS)
    f_idx = {f: i for i, f in enumerate(FUELS)}

    # forecasts from the tick the data describes, so stale data can be rolled forward
    age = max(0, t0 - snapshot.data_tick)
    mu_all = np.zeros((S, F, age + H))
    sd_all = np.zeros((S, F, age + H))
    for sid in stations:
        for fuel in FUELS:
            mu, sd = forecaster.forecast(snapshot, mm, sid, fuel, snapshot.data_tick + 1, age + H)
            mu_all[s_idx[sid], f_idx[fuel]] = mu
            sd_all[s_idx[sid], f_idx[fuel]] = sd

    # --- station outages and demand ------------------------------------
    station_open = np.ones((S, H), dtype=bool)
    demand_on = np.ones((S, H))
    for sid in stations:
        st = snapshot.stations[sid]
        windows = _disrupted_windows(snapshot.events, "station_outage", "station_ids", sid, t0,
                                     st.status == "OUTAGE")
        for j in range(H):
            station_open[s_idx[sid], j] = not _in_windows(t0 + j, windows)
            demand_on[s_idx[sid], j] = 0.0 if _in_windows(t0 + 1 + j, windows) else 1.0

    # --- existing shipments ----------------------------------------------
    intransit = np.zeros((S, F, H))
    in_transit_total = np.zeros((S, F))
    arrivals_during_age = np.zeros((S, F))
    route_by_id = {r.id: r for r in routes}
    for alloc in snapshot.open_allocations():
        s, f = s_idx.get(alloc.destination_station_id), f_idx[alloc.fuel_type]
        if s is None:
            continue
        route = route_by_id.get(alloc.route_id)
        transit = route.transit_ticks if route else 2
        eta = alloc.expected_arrival_tick or (alloc.created_tick + dd + transit)
        k = eta - t0 - 1
        in_transit_total[s, f] += alloc.quantity
        if k < 0 and eta > snapshot.data_tick:
            arrivals_during_age[s, f] += alloc.quantity  # landed while our data was stale
        else:
            intransit[s, f, min(max(k, 0), H - 1)] += alloc.quantity if k < H else 0.0

    depot_inv0 = np.array([[snapshot.depots[d].inventory.get(f, 0.0) for f in FUELS] for d in depots])
    for item in in_flight or []:
        s, d, f = s_idx.get(item.station), d_idx.get(item.depot), f_idx[item.fuel]
        if s is None or d is None:
            continue
        route = route_by_id.get(item.route)
        transit = route.transit_ticks if route else 2
        k = item.created_tick + dd + transit - t0 - 1
        if 0 <= k < H:
            intransit[s, f, k] += item.quantity
        in_transit_total[s, f] += item.quantity
        depot_inv0[d, f] = max(0.0, depot_inv0[d, f] - item.quantity)

    # --- nowcast: roll station inventory forward to t0 if data is old ------
    inv_data = np.array([[snapshot.stations[s].inventory.get(f, 0.0) for f in FUELS] for s in stations])
    cap = np.array([[snapshot.stations[s].capacity.get(f, 0.0) for f in FUELS] for s in stations])
    if age > 0:
        used = mu_all[:, :, :age].sum(axis=2)
        inv0 = np.clip(inv_data - used + arrivals_during_age, 0.0, cap)
        inv0_sd = np.sqrt((sd_all[:, :, :age] ** 2).sum(axis=2)) * np.sqrt(age)
    else:
        inv0 = inv_data
        inv0_sd = np.zeros((S, F))
    mu = mu_all[:, :, age:] * demand_on[:, None, :]
    sd = sd_all[:, :, age:] * demand_on[:, None, :]

    # --- depot supply ---------------------------------------------------
    supply = np.zeros((D, F, H))
    ratio = shortfall_ratio or {}
    pending_events = [e for e in snapshot.events if e.status == "SCHEDULED"
                      and e.type in ("shipment_delay", "supply_shortfall")]
    for arr in snapshot.arrivals:
        if arr.status == "ARRIVED" or arr.depot_id not in d_idx:
            continue
        planned, qty = arr.planned_tick, arr.quantity
        if arr.status == "DELAYED":
            qty *= ratio.get(arr.depot_id, 1.0)
        for event in pending_events:  # announced supply trouble hasn't touched the arrival yet
            params = event.parameters or {}
            if params.get("depot_ids") and arr.depot_id not in params["depot_ids"]:
                continue
            if params.get("fuel_types") and arr.fuel_type not in params["fuel_types"]:
                continue
            if event.type == "shipment_delay" and planned >= event.start_tick:
                planned += int(params.get("delay_ticks", 2))
            elif event.type == "supply_shortfall" and planned >= event.start_tick:
                qty *= float(params.get("factor", 0.5))
        j = planned - t0
        if 0 <= j < H:  # anything planned before t0 is already in the depot inventory
            supply[d_idx[arr.depot_id], f_idx[arr.fuel_type], j] += qty

    # --- dispatch capacity ------------------------------------------------
    dispatch_cap = np.zeros((D, H))
    for d in depots:
        depot = snapshot.depots[d]
        windows = _disrupted_windows(snapshot.events, "depot_constraint", "depot_ids", d, t0,
                                     depot.status == "CONSTRAINED")
        for j in range(H):
            factor = derate if _in_windows(t0 + j, windows) else 1.0
            dispatch_cap[d_idx[d], j] = depot.dispatch_capacity_per_tick * factor
    pending_now = np.zeros(D)
    for alloc in snapshot.allocations:
        if alloc.status == "PENDING" and alloc.created_tick == t0 and alloc.source_depot_id in d_idx:
            pending_now[d_idx[alloc.source_depot_id]] += alloc.quantity
    dispatch_cap[:, 0] = np.maximum(0.0, dispatch_cap[:, 0] - pending_now)

    # --- routes -----------------------------------------------------------
    route_ok = np.zeros((R, H), dtype=bool)
    available_routes = np.zeros(S)
    for r, route in enumerate(routes):
        windows = _disrupted_windows(snapshot.events, "route_disruption", "route_ids", route.id, t0,
                                     route.status == "DISRUPTED")
        s = s_idx[route.destination_station_id]
        for j in range(H):
            created, departs = t0 + j, t0 + j + dd
            route_ok[r, j] = (not _in_windows(created, windows) and not _in_windows(departs, windows)
                              and station_open[s, j])
        if route.status == "AVAILABLE":
            available_routes[s] += 1

    return Problem(
        t0=t0, lag=lag, H=H, dd=dd, stations=stations, depots=depots, routes=routes, fuels=FUELS,
        inv0=inv0, inv0_sd=inv0_sd, cap=cap, depot_inv0=depot_inv0, supply=supply, intransit=intransit,
        mu=mu, sd=sd, route_ok=route_ok, station_open=station_open, demand_on=demand_on,
        dispatch_cap=dispatch_cap,
        route_src=np.array([d_idx[r.source_depot_id] for r in routes]),
        route_dst=np.array([s_idx[r.destination_station_id] for r in routes]),
        route_transit=np.array([r.transit_ticks for r in routes]),
        route_max=np.array([r.max_shipment for r in routes], dtype=float),
        cross=np.array([snapshot.is_cross_region(r) for r in routes]),
        available_routes=available_routes, in_transit_total=in_transit_total,
        meta={"data_age": age},
    )
