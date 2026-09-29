"""A small stand-in for the organizer simulator, used by tests and local runs.

It follows the integration guide closely enough to exercise the agent: the
same world, the same validation order on POST /v1/allocations, idempotency,
created -> departed -> arrived timing, events and faults. It is not the real
simulator, and the real one always wins when they disagree (see tools/probes).

Run it standalone with:  python -m tests.fakesim --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tests import fixtures

PROFILES = {
    "urban_high": ({"DIESEL": 8500, "PETROL": 10500, "OCTANE": 5600}, 0.10, [(7, 10), (16, 21)], 1.45, 0.70),
    "industrial": ({"DIESEL": 14000, "PETROL": 4500, "OCTANE": 2200}, 0.08, [(6, 18)], 1.55, 0.45),
    "highway": ({"DIESEL": 10500, "PETROL": 11000, "OCTANE": 6200}, 0.12, [(6, 10), (16, 21)], 1.35, 0.75),
    "regional": ({"DIESEL": 7200, "PETROL": 7600, "OCTANE": 3600}, 0.10, [(7, 21)], 1.25, 0.65),
}


def _err(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message or code}}, status_code=status)


class World:
    def __init__(self, seed: int = 12345, tick_minutes: int = 15) -> None:
        self.seed = seed
        self.tick_minutes = tick_minutes
        self.reset()

    def reset(self) -> None:
        data = fixtures.payloads()
        self.tick = 0
        self.status = "PAUSED"
        self.regions = {r["id"]: r for r in data["regions"]}
        self.depots = {d["id"]: d for d in data["depots"]}
        self.stations = {s["id"]: s for s in data["stations"]}
        self.routes = {r["id"]: r for r in data["routes"]}
        self.arrivals = data["supply_arrivals"]
        self.events: list[dict[str, Any]] = []
        self.allocations: list[dict[str, Any]] = []
        self.demand: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []
        self.served = 0.0
        self.unmet = 0.0
        self.rng = random.Random(self.seed)
        self.subscribers: list[asyncio.Queue] = []

    # --- helpers -------------------------------------------------------------
    def sim_time(self, tick: int | None = None) -> str:
        t = self.tick if tick is None else tick
        minutes = t * self.tick_minutes
        day, rem = divmod(minutes, 1440)
        return f"2026-01-{1 + day:02d}T{rem // 60:02d}:{rem % 60:02d}:00+00:00"

    def publish(self, name: str, payload: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            if queue.full():
                self.subscribers.remove(queue)   # silently dropped, like the real one
            else:
                queue.put_nowait((name, payload))

    def active_fault(self, kind: str) -> dict[str, Any] | None:
        now = time.time()
        for fault in self.faults:
            if fault["type"] == kind and fault["active"] and fault["end"] > now:
                return fault
        return None

    def instance(self) -> dict[str, Any]:
        return {"id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": self.seed,
                "sim_time": self.sim_time(), "tick": self.tick, "tick_minutes": self.tick_minutes,
                "status": self.status}

    @staticmethod
    def _matches(event: dict[str, Any], key: str, value: str) -> bool:
        ids = event["parameters"].get(key) or []
        return not ids or value in ids

    # --- the tick ------------------------------------------------------------
    def tick_once(self) -> None:
        self.tick += 1
        t = self.tick
        for event in self.events:
            if event["status"] == "SCHEDULED" and event["start_tick"] <= t:
                event["status"] = "ACTIVE"
                self._apply(event, start=True)
            if event["status"] == "ACTIVE" and event["end_tick"] <= t:
                event["status"] = "RESOLVED"
                self._apply(event, start=False)
        for arr in self.arrivals:
            if arr["status"] != "ARRIVED" and arr["planned_tick"] <= t:
                depot = self.depots[arr["depot_id"]]
                fuel = arr["fuel_type"]
                depot["inventory"][fuel] = min(depot["capacity"][fuel], depot["inventory"][fuel] + arr["quantity"])
                arr["status"], arr["actual_tick"] = "ARRIVED", t
                self.publish("inventory.updated", {"entity_type": "depot", "entity_id": depot["id"],
                                                   "inventory": depot["inventory"]})
        for alloc in self.allocations:
            route = self.routes[alloc["route_id"]]
            if alloc["status"] == "PENDING" and alloc["created_tick"] < t:
                if route["status"] != "AVAILABLE":
                    alloc["status"], alloc["failure_reason"] = "FAILED", "ROUTE_DISRUPTED"
                else:
                    alloc["status"], alloc["departure_tick"] = "IN_TRANSIT", t
                    alloc["expected_arrival_tick"] = t + route["transit_ticks"]
                self.publish("allocation.status_changed", dict(alloc))
            if alloc["status"] == "IN_TRANSIT" and alloc["expected_arrival_tick"] <= t:
                station = self.stations[alloc["destination_station_id"]]
                fuel = alloc["fuel_type"]
                station["inventory"][fuel] = min(station["capacity"][fuel],
                                                 station["inventory"][fuel] + alloc["quantity"])
                alloc["status"], alloc["actual_arrival_tick"] = "ARRIVED", t
                self.publish("allocation.status_changed", dict(alloc))
        hour = ((t * self.tick_minutes) / 60.0) % 24
        for station in self.stations.values():
            daily, noise, busy, busy_f, off_f = PROFILES[station["demand_profile"]]
            factor = busy_f if any(a <= hour < b for a, b in busy) else off_f
            region = self.regions[station["region_id"]]["demand_factor"]
            for fuel in ("DIESEL", "PETROL", "OCTANE"):
                mean = daily[fuel] / (1440 / self.tick_minutes) * region * factor * station["demand_multiplier"]
                demand = max(0.0, mean * (1 + noise * self.rng.gauss(0, 1)))
                if station["status"] == "OUTAGE":
                    served = 0.0
                else:
                    served = min(station["inventory"][fuel], demand)
                    station["inventory"][fuel] -= served
                unmet = demand - served
                self.served += served
                self.unmet += unmet
                self.demand.append({"id": len(self.demand) + 1, "station_id": station["id"], "fuel_type": fuel,
                                    "tick": t, "sim_time": self.sim_time(), "demand_liters": round(demand, 3),
                                    "served_liters": round(served, 3), "unmet_liters": round(unmet, 3)})
        self.publish("simulation.tick", {"tick": t, "sim_time": self.sim_time()})

    def _apply(self, event: dict[str, Any], start: bool) -> None:
        p, kind = event["parameters"], event["type"]
        if kind == "demand_spike":
            mult = max(0.01, float(p.get("multiplier", 1.5)))
            for s in self.stations.values():
                if self._matches(event, "station_ids", s["id"]) and self._matches(event, "region_ids", s["region_id"]):
                    s["demand_multiplier"] = s["demand_multiplier"] * mult if start else s["demand_multiplier"] / mult
        elif kind == "route_disruption":
            for r in self.routes.values():
                if self._matches(event, "route_ids", r["id"]):
                    r["status"] = "DISRUPTED" if start else "AVAILABLE"
        elif kind == "station_outage":
            for s in self.stations.values():
                if self._matches(event, "station_ids", s["id"]):
                    s["status"] = "OUTAGE" if start else "OPEN"
        elif kind == "depot_constraint":
            for d in self.depots.values():
                if self._matches(event, "depot_ids", d["id"]):
                    d["status"] = "CONSTRAINED" if start else "OPEN"
        elif start and kind in ("shipment_delay", "supply_shortfall"):
            for arr in self.arrivals:
                if arr["status"] == "ARRIVED" or not self._matches(event, "depot_ids", arr["depot_id"]) \
                        or not self._matches(event, "fuel_types", arr["fuel_type"]):
                    continue
                if kind == "shipment_delay":
                    arr["planned_tick"] += int(p.get("delay_ticks", 2))
                    arr["status"] = "DELAYED"
                else:
                    arr["quantity"] *= float(p.get("factor", 0.5))

    # --- allocations -----------------------------------------------------------
    def create_allocation(self, body: dict[str, Any]) -> JSONResponse:
        required = ("idempotency_key", "source_depot_id", "destination_station_id", "route_id", "fuel_type", "quantity")
        if any(k not in body for k in required) or body["fuel_type"] not in ("DIESEL", "PETROL", "OCTANE") \
                or not isinstance(body["quantity"], (int, float)) or body["quantity"] <= 0 \
                or not (1 <= len(str(body["idempotency_key"])) <= 150):
            return JSONResponse({"detail": [{"msg": "validation error"}]}, status_code=422)
        for alloc in self.allocations:
            if alloc["idempotency_key"] == body["idempotency_key"]:
                same = all(alloc[k] == body[k] for k in required[1:5]) and float(alloc["quantity"]) == float(body["quantity"])
                return JSONResponse(alloc, status_code=201) if same else _err(409, "IDEMPOTENCY_KEY_MISMATCH")
        depot = self.depots.get(body["source_depot_id"])
        station = self.stations.get(body["destination_station_id"])
        route = self.routes.get(body["route_id"])
        fuel, qty = body["fuel_type"], float(body["quantity"])
        if not depot or not station or not route:
            return _err(404, "NOT_FOUND")
        if route["source_depot_id"] != depot["id"] or route["destination_station_id"] != station["id"]:
            return _err(409, "ROUTE_MISMATCH")
        if depot["status"] not in ("OPEN", "CONSTRAINED"):
            return _err(409, "DEPOT_CLOSED")
        if station["status"] != "OPEN":
            return _err(409, "STATION_CLOSED")
        if route["status"] != "AVAILABLE":
            return _err(409, "ROUTE_DISRUPTED")
        if qty > route["max_shipment"]:
            return _err(409, "ROUTE_CAPACITY_EXCEEDED")
        if depot["inventory"][fuel] < qty:
            return _err(409, "INSUFFICIENT_INVENTORY")
        used = sum(a["quantity"] for a in self.allocations
                   if a["source_depot_id"] == depot["id"] and a["created_tick"] == self.tick
                   and a["status"] in ("PENDING", "IN_TRANSIT"))
        if used + qty > depot["dispatch_capacity_per_tick"]:
            return _err(409, "DISPATCH_CAPACITY_EXCEEDED")
        if station["inventory"][fuel] + qty > station["capacity"][fuel]:
            return _err(409, "DESTINATION_CAPACITY_EXCEEDED")
        depot["inventory"][fuel] -= qty
        alloc = {"id": len(self.allocations) + 1, **{k: body[k] for k in required}, "quantity": qty,
                 "created_tick": self.tick, "departure_tick": None, "expected_arrival_tick": None,
                 "actual_arrival_tick": None, "status": "PENDING", "failure_reason": None}
        self.allocations.append(alloc)
        self.publish("allocation.status_changed", dict(alloc))
        return JSONResponse(alloc, status_code=201)


def create_app(world: World | None = None, speed: float = 8.0) -> FastAPI:
    world = world or World()
    app = FastAPI(title="fake BUP simulator")
    app.state.world = world
    app.state.speed = speed

    @app.middleware("http")
    async def faults(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/") and path != "/v1/health":
            latency = world.active_fault("latency")
            if latency:
                await asyncio.sleep(latency["parameters"].get("delay_ms", 500) / 1000)
            if world.active_fault("unavailable"):
                return JSONResponse({"error": {"code": "FAULT_INJECTED", "message": "Simulator API temporarily unavailable."}}, 503)
            rate = world.active_fault("error_rate")
            if rate and world.rng.random() < rate["parameters"].get("rate", 0.25) and path != "/v1/stream":
                return JSONResponse({"error": {"code": "FAULT_INJECTED", "message": "Injected transient API error."}}, 503)
            if path == "/v1/stream" and world.active_fault("stream_disconnect"):
                return JSONResponse({"detail": {"code": "FAULT_INJECTED"}}, 503)
        response = await call_next(request)
        if path.startswith("/v1/") and request.method == "GET" and path != "/v1/stream" \
                and world.active_fault("stale_data"):
            response.headers["X-Simulator-Stale"] = "true"
        return response

    @app.get("/v1/health")
    async def health():
        return {"status": "ok", "database": "ok", "simulation": {"status": world.status, "tick": world.tick}}

    @app.get("/v1/instance")
    async def instance():
        return world.instance()

    @app.get("/v1/regions")
    async def regions():
        return list(world.regions.values())

    @app.get("/v1/depots")
    async def depots():
        return list(world.depots.values())

    @app.get("/v1/stations")
    async def stations():
        return list(world.stations.values())

    @app.get("/v1/routes")
    async def routes():
        return list(world.routes.values())

    @app.get("/v1/supply-arrivals")
    async def arrivals():
        return sorted(world.arrivals, key=lambda a: a["planned_tick"])

    @app.get("/v1/events")
    async def events():
        return sorted(world.events, key=lambda e: -e["id"])

    @app.get("/v1/allocations")
    async def allocations():
        return sorted(world.allocations, key=lambda a: -a["id"])

    @app.get("/v1/demand-history")
    async def demand(limit: int = 200, station_id: str | None = None):
        rows = [r for r in world.demand if station_id is None or r["station_id"] == station_id]
        return list(reversed(rows[-max(1, min(2000, limit)):]))

    @app.get("/v1/metrics")
    async def metrics():
        total = world.served + world.unmet
        return {"served_demand_liters": round(world.served, 3), "unmet_demand_liters": round(world.unmet, 3),
                "service_level": round(world.served / total, 6) if total else 1.0,
                "allocation_liters": sum(a["quantity"] for a in world.allocations
                                         if a["status"] in ("IN_TRANSIT", "ARRIVED")),
                "allocation_failures": sum(1 for a in world.allocations if a["status"] == "FAILED")}

    @app.post("/v1/allocations")
    async def create(request: Request):
        return world.create_allocation(await request.json())

    @app.post("/v1/allocations/{alloc_id}/cancel")
    async def cancel(alloc_id: int):
        for alloc in world.allocations:
            if alloc["id"] == alloc_id:
                if alloc["status"] != "PENDING":
                    return _err(409, "CANNOT_CANCEL")
                world.depots[alloc["source_depot_id"]]["inventory"][alloc["fuel_type"]] += alloc["quantity"]
                alloc["status"] = "CANCELLED"
                return alloc
        return _err(404, "ALLOCATION_NOT_FOUND")

    @app.get("/v1/stream")
    async def stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        world.subscribers.append(queue)

        async def gen():
            yield ": connected\n\n"
            try:
                while True:
                    try:
                        name, payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {name}\ndata: {json.dumps(payload)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                if queue in world.subscribers:
                    world.subscribers.remove(queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- admin -----------------------------------------------------------------
    @app.post("/admin/run")
    async def run():
        world.status = "RUNNING"
        return world.instance()

    @app.post("/admin/pause")
    async def pause():
        world.status = "PAUSED"
        return world.instance()

    @app.post("/admin/step")
    async def step():
        world.tick_once()
        return {"tick": world.tick, "sim_time": world.sim_time()}

    @app.post("/admin/reset")
    async def reset():
        subscribers = world.subscribers
        world.reset()
        world.subscribers = subscribers
        world.publish("simulator.notice", {"message": "Simulation reset"})
        return {"status": "reset"}

    @app.post("/admin/events")
    async def add_event(request: Request):
        body = await request.json()
        event = {"id": len(world.events) + 1, "type": body["type"], "start_tick": body["start_tick"],
                 "end_tick": body["start_tick"] + body["duration_ticks"], "status": "SCHEDULED",
                 "parameters": body.get("parameters") or {}}
        world.events.append(event)
        if event["start_tick"] <= world.tick:
            event["status"] = "ACTIVE"
            world._apply(event, start=True)
        return JSONResponse(event, status_code=201)

    @app.post("/admin/faults")
    async def add_fault(request: Request):
        body = await request.json()
        fault = {"id": len(world.faults) + 1, "type": body["type"], "parameters": body.get("parameters") or {},
                 "active": True, "end": time.time() + float(body["duration_seconds"])}
        world.faults.append(fault)
        return JSONResponse(fault, status_code=201)

    @app.post("/admin/faults/clear")
    async def clear():
        for fault in world.faults:
            fault["active"] = False
        return {"status": "cleared"}

    @app.on_event("startup")
    async def runner() -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(1.0 / app.state.speed if app.state.speed > 0 else 1.0)
                if world.status == "RUNNING":
                    world.tick_once()
        app.state.runner = asyncio.create_task(loop())

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--speed", type=float, default=8.0)
    parser.add_argument("--running", action="store_true")
    args = parser.parse_args()
    w = World()
    if args.running:
        w.status = "RUNNING"
    uvicorn.run(create_app(w, args.speed), host="0.0.0.0", port=args.port, log_level="warning")
