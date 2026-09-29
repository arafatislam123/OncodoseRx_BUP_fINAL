"""Detectors from design section 8.3.

Each check raises alerts through the AlertManager. The detector itself keeps
only the small amount of state it needs (CUSUM sums, first-seen supply plans).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np

from agent.intelligence.alerts import AlertManager
from agent.intelligence.forecaster import Forecaster, Observation
from agent.intelligence.world import FUELS
from agent.sync.models import DemandObs
from agent.sync.snapshot import Snapshot


@dataclass
class Runway:
    depot: str
    fuel: str
    inventory: float
    draw_per_tick: float
    runway_ticks: float
    next_arrival_tick: int | None
    ticks_to_next: int | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _name(entity_id: str) -> str:
    return entity_id.split("-", 1)[-1].replace("coxsbazar", "cox's bazar").title()


class Detector:
    def __init__(self, alerts: AlertManager, forecaster: Forecaster, policy: dict[str, Any] | None = None) -> None:
        p = policy or {}
        self.alerts = alerts
        self.forecaster = forecaster
        self.warn = float(p.get("stockout_warn", 0.2))
        self.crit = float(p.get("stockout_crit", 0.5))
        self.warn_clear = float(p.get("stockout_warn_clear", 0.1))
        self.crit_clear = float(p.get("stockout_crit_clear", 0.3))
        self.cusum_k = float(p.get("cusum_k", 0.5))
        self.cusum_h = float(p.get("cusum_h", 5.0))
        self.z_alert = float(p.get("z_alert", 3.0))
        self.runway_buffer = int(p.get("runway_buffer_ticks", 8))
        self._cusum: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self._ratio: dict[tuple[str, str], float] = {}
        self._anomaly_until: dict[tuple[str, str], tuple[int, str, dict[str, Any]]] = {}
        self._first_plan: dict[str, tuple[int, float]] = {}
        self._served: dict[tuple[str, str, int], float] = {}
        self.runways: list[Runway] = []

    # --- stockout risk ---------------------------------------------------
    def stockout(self, tick: int, no_action: dict[tuple[str, str], Any], snapshot: Snapshot) -> None:
        for (sid, fuel), result in no_action.items():
            existing = self.alerts.is_open("stockout_risk", sid, fuel)
            prev = existing.severity if existing else None
            p = result.p_stockout
            if p >= self.crit or (prev == "crit" and p >= self.crit_clear):
                severity = "crit"
            elif p >= self.warn or (prev in ("warn", "crit") and p >= self.warn_clear):
                severity = "warn"
            else:
                continue
            hours = result.time_to_stockout_ticks * snapshot.tick_minutes / 60 if result.time_to_stockout_ticks else None
            when = f"in about {hours:.1f} h" if hours is not None else "within the horizon"
            self.alerts.raise_(
                "stockout_risk", sid, fuel, severity,
                f"{_name(sid)} {fuel.lower()} may run out {when} (risk {p:.0%}).", tick,
                {"p_stockout": round(p, 3), "time_to_stockout_ticks": result.time_to_stockout_ticks,
                 "inventory": snapshot.stations[sid].inventory.get(fuel, 0.0)},
            )

    # --- demand anomalies -----------------------------------------------
    def demand(self, tick: int, observations: list[Observation]) -> None:
        for obs in observations:
            if obs.censored or obs.mean <= 0:
                continue
            key = (obs.station, obs.fuel)
            pos_neg = self._cusum[key]
            pos_neg[0] = max(0.0, pos_neg[0] + obs.z - self.cusum_k)
            pos_neg[1] = max(0.0, pos_neg[1] - obs.z - self.cusum_k)
            ratio = self._ratio.get(key, 1.0)
            self._ratio[key] = 0.8 * ratio + 0.2 * (obs.demand / obs.mean)
            fired = pos_neg[0] > self.cusum_h or pos_neg[1] > self.cusum_h
            if fired:
                direction = "above" if pos_neg[0] > self.cusum_h else "below"
                msg = (f"Demand at {_name(obs.station)} {obs.fuel.lower()} is running {self._ratio[key]:.1f}x "
                       f"forecast ({direction}), no known event explains it.")
                self._anomaly_until[key] = (tick + 8, msg, {"ratio": round(self._ratio[key], 2),
                                                            "cusum": [round(v, 2) for v in pos_neg]})
                self.forecaster.reset_changepoint(obs.station, obs.fuel)  # re-learn quickly
                pos_neg[0] = pos_neg[1] = 0.0
            elif abs(obs.z) >= self.z_alert and key not in self._anomaly_until:
                msg = f"Single-tick demand spike at {_name(obs.station)} {obs.fuel.lower()} (z={obs.z:.1f})."
                self._anomaly_until[key] = (tick + 2, msg, {"z": round(obs.z, 2)})
        for key, (until, msg, evidence) in list(self._anomaly_until.items()):
            if tick > until:
                del self._anomaly_until[key]
                continue
            severity = "warn" if "running" in msg else "info"
            self.alerts.raise_("demand_anomaly", key[0], key[1], severity, msg, tick, evidence)

    # --- inventory balance ----------------------------------------------
    def remember_demand(self, rows: list[DemandObs]) -> None:
        for row in rows:
            self._served[(row.station_id, row.fuel_type, row.tick)] = row.served_liters
        if len(self._served) > 20_000:
            newest = max(t for _, _, t in self._served)
            self._served = {k: v for k, v in self._served.items() if k[2] > newest - 200}

    def inventory_balance(self, tick: int, prev: Snapshot | None, cur: Snapshot) -> None:
        if prev is None or prev.stale or cur.stale or prev.epoch != cur.epoch:
            return
        t0, t1 = prev.data_tick, cur.data_tick
        if not (0 < t1 - t0 <= 8):
            return
        for sid, station in cur.stations.items():
            before = prev.stations.get(sid)
            if before is None:
                continue
            for fuel in FUELS:
                cap = station.capacity.get(fuel, 0.0)
                if station.inventory.get(fuel, 0.0) > 0.97 * cap:
                    continue  # overflow rules make the balance fuzzy near the top
                arrived = sum(a.quantity for a in cur.allocations
                              if a.destination_station_id == sid and a.fuel_type == fuel
                              and a.status == "ARRIVED" and a.actual_arrival_tick is not None
                              and t0 < a.actual_arrival_tick <= t1)
                delta = station.inventory.get(fuel, 0.0) - before.inventory.get(fuel, 0.0)
                windows = [range(t0 + 1, t1 + 1), range(t0, t1)]
                errors = []
                for window in windows:
                    served = [self._served.get((sid, fuel, t)) for t in window]
                    if any(v is None for v in served):
                        break
                    errors.append(abs(delta - (arrived - sum(served))))  # type: ignore[arg-type]
                if len(errors) < 2:
                    continue
                tolerance = max(5.0, 0.02 * (arrived + abs(delta)))
                if min(errors) > tolerance:
                    self.alerts.raise_(
                        "inventory_mismatch", sid, fuel, "info",
                        f"Unexplained inventory change at {_name(sid)} {fuel.lower()} ({min(errors):.0f} L).",
                        tick, {"delta": round(delta, 1), "arrived": arrived, "error": round(min(errors), 1)},
                    )

    # --- supply ---------------------------------------------------------
    def supply(self, tick: int, snapshot: Snapshot) -> None:
        for arrival in snapshot.arrivals:
            first = self._first_plan.setdefault(arrival.id, (arrival.planned_tick, arrival.quantity))
            if arrival.status == "ARRIVED":
                continue
            slip = arrival.planned_tick - first[0]
            if arrival.status == "DELAYED" or slip > 0:
                self.alerts.raise_(
                    "supply_delay", arrival.depot_id, arrival.fuel_type, "warn",
                    f"{_name(arrival.depot_id)} {arrival.fuel_type.lower()} supply slipped {slip} ticks "
                    f"(now due at tick {arrival.planned_tick}).",
                    tick, {"arrival_id": arrival.id, "planned_tick": arrival.planned_tick, "slip": slip},
                )
            if arrival.quantity < first[1] - 1:
                self.alerts.raise_(
                    "supply_shortfall", arrival.depot_id, arrival.fuel_type, "warn",
                    f"{_name(arrival.depot_id)} {arrival.fuel_type.lower()} supply cut from "
                    f"{first[1]:,.0f} L to {arrival.quantity:,.0f} L.",
                    tick, {"arrival_id": arrival.id, "original": first[1], "now": arrival.quantity},
                )

    def depot_runway(self, tick: int, snapshot: Snapshot,
                     forecasts: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]) -> list[Runway]:
        runways: list[Runway] = []
        for depot in snapshot.depots.values():
            region_stations = [s.id for s in snapshot.stations.values() if s.region_id == depot.region_id]
            for fuel in FUELS:
                draw = sum(float(np.mean(forecasts[(sid, fuel)][0])) for sid in region_stations
                           if (sid, fuel) in forecasts)
                inventory = depot.inventory.get(fuel, 0.0)
                runway = inventory / draw if draw > 0 else float("inf")
                upcoming = [a.planned_tick for a in snapshot.arrivals
                            if a.depot_id == depot.id and a.fuel_type == fuel and a.status != "ARRIVED"
                            and a.planned_tick >= tick]
                nxt = min(upcoming) if upcoming else None
                to_next = nxt - tick if nxt is not None else None
                runways.append(Runway(depot.id, fuel, inventory, draw, runway, nxt, to_next))
                if to_next is None:
                    continue
                if runway < to_next:
                    severity = "crit"
                elif runway < to_next + self.runway_buffer:
                    severity = "warn"
                else:
                    continue
                days = runway * snapshot.tick_minutes / 60 / 24
                self.alerts.raise_(
                    "depot_runway", depot.id, fuel, severity,
                    f"{_name(depot.id)} {fuel.lower()} runway is {days:.1f} days, next supply in {to_next} ticks.",
                    tick, {"runway_ticks": round(runway, 1), "ticks_to_next": to_next},
                )
        self.runways = runways
        return runways

    # --- network status and bottlenecks -----------------------------------
    def network(self, tick: int, snapshot: Snapshot) -> None:
        for route in snapshot.routes.values():
            if route.status == "DISRUPTED":
                self.alerts.raise_("route_disrupted", route.id, None, "warn",
                                   f"Route {route.id.replace('route-', '')} is disrupted.", tick)
        for station in snapshot.stations.values():
            if station.status == "OUTAGE":
                self.alerts.raise_("station_outage", station.id, None, "warn",
                                   f"{_name(station.id)} is in outage.", tick)
            total = len(snapshot.routes_to(station.id))
            available = len(snapshot.routes_to(station.id, only_available=True))
            if available < total:
                severity = "crit" if available == 0 else "warn"
                self.alerts.raise_("bottleneck", station.id, None, severity,
                                   f"{_name(station.id)} has {available} of {total} routes available.", tick,
                                   {"available_routes": available, "total_routes": total})
        for depot in snapshot.depots.values():
            if depot.status == "CONSTRAINED":
                self.alerts.raise_("depot_constrained", depot.id, None, "warn",
                                   f"{_name(depot.id)} is constrained.", tick)
        for event in snapshot.events:
            if event.status in ("SCHEDULED", "ACTIVE"):
                verb = "starts" if event.status == "SCHEDULED" else "active until"
                at = event.start_tick if event.status == "SCHEDULED" else event.end_tick
                self.alerts.raise_("event", f"event-{event.id}", None, "info",
                                   f"{event.type.replace('_', ' ')} {verb} tick {at}.", tick,
                                   {"event_id": event.id, "parameters": event.parameters})
