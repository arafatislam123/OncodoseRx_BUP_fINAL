"""The agent's control loop: Observe, Detect, Predict, Decide, Simulate, Act, Monitor, Recover.

One call to run_cycle() does a full pass (design section 5). The agent keeps
working without Postgres, the API or the LLM: everything it needs to decide
lives in memory and in its own SQLite file.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from agent import metrics
from agent.config import Settings
from agent.control.mode import ModeController, Transition
from agent.control.timing import Timing
from agent.decisions import build_record, explain
from agent.execution.executor import Executor, Submission
from agent.execution.gate import ApprovalQueue, Gate, GateInput, Recommendation
from agent.execution.intent_log import IntentLog
from agent.execution.keys import idempotency_key
from agent.intelligence.alerts import AlertManager
from agent.intelligence.confidence import score as confidence_score
from agent.intelligence.detector import Detector
from agent.intelligence.forecaster import Forecaster
from agent.intelligence.multipliers import MultiplierModel
from agent.intelligence.orders import PlannedOrder, PlanResult, orders_to_x, to_orders
from agent.intelligence.planner_heuristic import solve_heuristic
from agent.intelligence.planner_lp import solve_lp
from agent.intelligence.problem import Problem, build_problem, problem_to_dict
from agent.intelligence.projection import ProjectionResult, plan_arrivals, project
from agent.intelligence.world import FUELS, WorldModel
from agent.sync.cache import WorldCache
from agent.sync.client import ApiHealth, SimClient
from agent.sync.snapshot import Snapshot
from agent.sync.state_sync import StateSync

log = structlog.get_logger()


@dataclass
class CycleView:
    """What the last cycle saw and decided. Served to the API through /snapshot."""

    cycle_id: str = ""
    tick: int = -1
    snapshot: Snapshot | None = None
    problem: Problem | None = None
    no_action: dict[tuple[str, str], ProjectionResult] = field(default_factory=dict)
    with_plan: dict[tuple[str, str], ProjectionResult] = field(default_factory=dict)
    policy_used: str = ""
    fallback: bool = False
    orders: list[PlannedOrder] = field(default_factory=list)
    outlook: dict[tuple[str, str], ProjectionResult] = field(default_factory=dict)  # full horizon, no action
    duration_s: float = 0.0


class Agent:
    def __init__(self, settings: Settings | None = None, client: SimClient | None = None) -> None:
        self.s = settings or Settings()
        stepped = self.s.run_mode == "stepped"
        self.health = client.health if client else ApiHealth()
        self.client = client or SimClient(self.s.simulator_url, self.health)
        self.cache = WorldCache()
        facts = self.s.facts
        self.sync = StateSync(self.client, self.cache, float(facts.get("alloc_reconcile_interval_s", 2.0)),
                              on_reset=self._on_reset, on_invalid=self._on_invalid)
        self.world = WorldModel(self.s.prior, self.s.tick_minutes)
        self.forecaster = self._new_forecaster()
        self.boot_id = format(int(time.time()) % 10**8, "x")
        self.alerts = AlertManager(id_prefix=f"{self.boot_id}-")
        self.detector = Detector(self.alerts, self.forecaster, self.s.policy.get("detector"))
        self.intents = IntentLog(self.s.data_dir / "agent.sqlite")
        self.executor = Executor(self.client, self.intents, sequential=stepped)
        self.timing = Timing(self.s.simulation_speed, stepped=stepped)
        self.modes = ModeController(self.s.policy.get("modes"))
        self.gate = Gate({**self.s.policy.get("gate", {}), "confidence_review_threshold": self.s.confidence_threshold})
        self.queue = ApprovalQueue(self.s.approval_ttl_ticks, float(self.s.p("gate", "requantify_tolerance", 0.1)),
                                   next_id=lambda: f"rec-{self._next_counter('rec_seq'):06d}")
        self.autonomy = self.s.autonomy
        self.policy_name = self.s.planner_policy
        self.view = CycleView()
        self.prev_snapshot: Snapshot | None = None
        self.recent_decisions: deque[dict[str, Any]] = deque(maxlen=200)
        self.recent_transitions: deque[dict[str, Any]] = deque(maxlen=100)
        self.last_cycle_tick = -1
        self.last_cycle_wall = 0.0
        self.cycles = 0
        self.forecaster_ok = True
        self.planner_ok = True
        self.lock = asyncio.Lock()
        self._cycle_ids = itertools.count(1)
        self._pending_exec_alerts: list[tuple[str, str, str]] = []
        self._restore()

    # --- setup and recovery -----------------------------------------------
    def _new_forecaster(self) -> Forecaster:
        return Forecaster(self.world, self.s.policy.get("forecaster"),
                          censoring_rule=str(self.s.facts.get("censoring_rule", "exclude_stockout")))

    def _restore(self) -> None:
        recovered = self.intents.recover_after_crash()
        if recovered:
            log.warning("agent.recovered_intents", count=recovered)
        self.forecaster.load_state(self.intents.load_model_state())
        self.queue.restore(self.intents.pending_recommendations())
        self.sync.epoch = int(self.intents.kv_get("epoch", "0") or 0)
        self.autonomy = self.intents.kv_get("autonomy", self.autonomy) or self.autonomy
        if self.intents.kv_get("manual", "0") == "1":
            self.modes.set_manual(True, 0, "restored")

    def _next_counter(self, name: str) -> int:
        value = int(self.intents.kv_get(name, "0") or 0) + 1
        self.intents.kv_set(name, str(value))
        return value

    async def _on_reset(self, reason: str) -> None:
        self.intents.kv_set("epoch", str(self.sync.epoch))
        self.prev_snapshot = None
        self.alerts.begin_cycle()
        self.alerts.raise_("simulator_reset", "simulator", None, "info", f"Simulator reset detected ({reason}).",
                           0, {"epoch": self.sync.epoch})

    def _on_invalid(self, resource: str, message: str) -> None:
        self._pending_exec_alerts.append(("warn", "INVALID_RESPONSE",
                                          f"Rejected an invalid {resource} response from the simulator."))

    # --- operator controls ------------------------------------------------
    def set_autonomy(self, autonomy: str) -> None:
        if autonomy not in ("autopilot", "copilot", "manual"):
            raise ValueError("autonomy must be autopilot, copilot or manual")
        self.autonomy = autonomy
        self.intents.kv_set("autonomy", autonomy)

    def set_manual(self, on: bool, who: str) -> None:
        transition = self.modes.set_manual(on, max(self.last_cycle_tick, 0), who)
        self.intents.kv_set("manual", "1" if on else "0")
        if transition:
            self._record_transition(transition)

    def set_policy(self, planner_policy: str | None = None, config: dict[str, Any] | None = None) -> None:
        if planner_policy:
            if planner_policy not in ("lp", "heuristic"):
                raise ValueError("planner policy must be lp or heuristic")
            self.policy_name = planner_policy
        if config:
            for section, values in config.items():
                if isinstance(values, dict):
                    self.s.policy.setdefault(section, {}).update(values)
                else:
                    self.s.policy[section] = values

    def review(self, rec_id: str, approve: bool, reviewer: str, note: str | None = None) -> Recommendation | None:
        rec = self.queue.review(rec_id, approve, reviewer, note)
        if rec is not None:
            self._persist_recommendations()
        return rec

    # --- the cycle ----------------------------------------------------------
    async def run_cycle(self) -> CycleView | None:
        async with self.lock:
            started = time.perf_counter()
            try:
                view = await self._cycle()
            finally:
                elapsed = time.perf_counter() - started
                metrics.CYCLE_DURATION.observe(elapsed)
                self.timing.observe_cycle(elapsed)
            if view is not None:
                view.duration_s = elapsed
            return view

    async def _cycle(self) -> CycleView | None:
        deadline = time.monotonic() + max(0.5, 2 * min(self.timing.tick_period_s, 5.0))
        await self.sync.refresh(deadline)
        snap = self.sync.snapshot()
        if snap is None:
            self._update_mode(None)
            return None
        self.timing.running = snap.status == "RUNNING"

        cycle_id = f"cyc-{self.boot_id}-{next(self._cycle_ids)}"
        structlog.contextvars.bind_contextvars(cycle_id=cycle_id, tick=snap.tick)
        if self.last_cycle_tick >= 0 and snap.tick > self.last_cycle_tick + 1:
            metrics.CYCLE_SKIPPED.inc(snap.tick - self.last_cycle_tick - 1)

        # --- recover: settle anything we sent but never heard back about ---
        if self.cache.entries["allocations"].fetched_tick >= 0:
            for outcome in await self.executor.reconcile(snap.allocations, snap.tick):
                self._record_outcome(outcome.key, outcome.state, outcome.allocation_id)

        # --- observe + predict ------------------------------------------------
        mm = MultiplierModel(snap, self.world.baseline_multiplier)
        if not mm.consistent:
            await self.sync.refresh(deadline)          # read events and stations once more
            snap = self.sync.snapshot() or snap
            mm = MultiplierModel(snap, self.world.baseline_multiplier)
        rows = self.sync.take_new_demand()
        observations = []
        try:
            observations = self.forecaster.observe(rows, snap, mm)
            self.forecaster_ok = True
        except Exception:
            log.exception("forecaster.failed")
            metrics.FALLBACKS.labels("forecaster").inc()
            self.forecaster_ok = False
            self.forecaster = self._new_forecaster()   # structural prior only
            self.detector.forecaster = self.forecaster
        self.detector.remember_demand(rows)

        self._update_mode(snap)
        mode = self.modes.mode

        lag = self.timing.land_lag(self.client.post_latency.quantile(0.9, 0.05))
        metrics.LAND_LAG.set(lag)
        known_keys = {a.idempotency_key for a in snap.allocations}
        horizon = int(self.s.p("planner", "horizon_ticks", self.s.horizon_ticks))
        try:
            problem = build_problem(snap, self.forecaster, mm, horizon=horizon, lag=lag, facts=self.s.facts,
                                    in_flight=self.executor.in_flight(known_keys))
        except Exception:
            log.exception("problem.build_failed")
            metrics.FALLBACKS.labels("forecaster").inc()
            self.forecaster = self._new_forecaster()
            self.detector.forecaster = self.forecaster
            problem = build_problem(snap, self.forecaster, mm, horizon=horizon, lag=lag, facts=self.s.facts,
                                    in_flight=self.executor.in_flight(known_keys))

        samples = int(self.s.p("projection", "samples", 200))
        overflow = str(self.s.facts.get("overflow_policy", "lost"))
        window = min(problem.H, int(self.s.p("projection", "risk_window_ticks", 16)))
        self._window = window
        # risk is judged over lead time plus review; the full horizon is only for the charts
        no_action = project(problem, None, samples=samples, overflow=overflow, horizon=window)
        outlook = project(problem, None, samples=samples, overflow=overflow, bands=True)

        # --- decide ---------------------------------------------------------
        plan, heuristic, fallback = await self._plan(problem, mode)
        lead = {sid: min((problem.arrival_index(r, lag) + 1 for r in range(problem.R)
                          if problem.route_dst[r] == s), default=4)
                for s, sid in enumerate(problem.stations)}
        critical = {key for key, res in no_action.items()
                    if res.p_stockout >= 0.5 or (res.time_to_stockout_ticks is not None
                                                 and res.time_to_stockout_ticks <= lead[key[0]] + 2)}
        orders = to_orders(problem, plan, critical, float(self.s.p("planner", "bucket_liters", 250)),
                           float(self.s.p("planner", "min_shipment", 500)))
        if mode in ("DEGRADED", "SAFE"):
            orders = [o for o in orders if not o.cross_region or (o.station_id, o.fuel) in critical]

        # --- simulate ---------------------------------------------------------
        later = {k: v for k, v in plan.x.items() if k[2] != problem.lag}
        full = {**later}
        for key, value in orders_to_x(problem, orders).items():
            full[key] = full.get(key, 0.0) + value
        with_plan = project(problem, plan_arrivals(problem, full), samples=samples, overflow=overflow,
                            horizon=window)

        # --- detect -----------------------------------------------------------
        self.alerts.begin_cycle()
        tick = snap.tick
        self.detector.stockout(tick, no_action, snap)
        self.detector.demand(tick, observations)
        self.detector.inventory_balance(tick, self.prev_snapshot, snap)
        self.detector.supply(tick, snap)
        forecasts = {(sid, fuel): (problem.mu[s, f], problem.sd[s, f])
                     for s, sid in enumerate(problem.stations) for f, fuel in enumerate(problem.fuels)}
        self.detector.depot_runway(tick, snap, forecasts)
        self.detector.network(tick, snap)
        self._raise_exec_alerts(tick)
        if fallback:
            self.alerts.raise_("fallback_active", "planner", None, "warn",
                               "The LP planner failed; the heuristic fallback is planning this cycle.", tick)
        if mode != "NORMAL":
            self.alerts.raise_("mode", "agent", None, "warn" if mode != "MANUAL" else "info",
                               f"Agent is in {mode} mode.", tick)
        self.alerts.end_cycle(tick)

        # --- act ----------------------------------------------------------------
        heur_totals = heuristic.total_by_station(problem, lag) if heuristic else {}
        submissions, _ = self._decide(snap, problem, mode, plan, fallback, orders, no_action, with_plan,
                                             heur_totals, lead, cycle_id, mm)
        submissions += self._approved_submissions(problem, orders, snap)
        self.queue.expire(tick)
        outcomes = await self.executor.submit(submissions, snap.epoch)
        for outcome in outcomes:
            self._record_outcome(outcome.key, outcome.state, outcome.allocation_id, outcome.code)
            if outcome.created_tick is not None:
                self.timing.observe_created(problem.t0 + problem.lag, outcome.created_tick)
        events = self.executor.take_events()
        if events.resync or events.reconcile:
            self.sync.request_reconcile()
        self._pending_exec_alerts.extend(events.alerts)

        # --- monitor ------------------------------------------------------------
        self._persist(snap, problem, cycle_id, rows)
        self._publish(snap, problem, no_action)
        self.prev_snapshot = snap
        self.last_cycle_tick = tick
        self.last_cycle_wall = time.time()
        self.cycles += 1
        self.view = CycleView(cycle_id, tick, snap, problem, no_action, with_plan, plan.policy, fallback, orders,
                              outlook=outlook)
        log.info("cycle.completed", orders=len(orders), sent=len(submissions), mode=mode, lag=lag,
                 policy=plan.policy)
        structlog.contextvars.unbind_contextvars("cycle_id", "tick")
        return self.view

    async def _plan(self, problem: Problem, mode: str) -> tuple[PlanResult, PlanResult | None, bool]:
        planner_policy = self.s.policy.get("planner", {})
        heuristic = solve_heuristic(problem, planner_policy)
        if self.policy_name != "lp" or mode == "SAFE":
            self.planner_ok = True
            return heuristic, heuristic, False
        try:
            limit = int(planner_policy.get("time_limit_ms", 150))
            if self.s.run_mode == "stepped":
                limit = max(limit, 2000)   # no clock pressure when stepping; keep results reproducible
            plan = await asyncio.to_thread(solve_lp, problem, planner_policy, limit)
        except Exception:
            log.exception("planner.lp_crashed")
            plan = PlanResult("lp", "failed", 0.0)
        metrics.PLANNER_SECONDS.observe(plan.solve_seconds)
        if plan.status == "failed":
            metrics.FALLBACKS.labels("planner").inc()
            log.warning("fallback.activated", reason="planner")
            self.planner_ok = False
            return heuristic, heuristic, True
        self.planner_ok = True
        return plan, heuristic, False

    def _decide(self, snap: Snapshot, problem: Problem, mode: str, plan: PlanResult, fallback: bool,
                orders: list[PlannedOrder], no_action: dict, with_plan: dict, heur_totals: dict,
                lead: dict[str, int], cycle_id: str,
                mm: MultiplierModel) -> tuple[list[Submission], set[tuple[str, str, str]]]:
        submissions: list[Submission] = []
        reviewed: set[tuple[str, str, str]] = set()
        groups: dict[tuple[str, str, str], list[PlannedOrder]] = {}
        for order in orders:
            groups.setdefault((order.station_id, order.fuel, order.route_id), []).append(order)

        for (station, fuel, route), group in groups.items():
            s, f = problem.stations.index(station), problem.fuels.index(fuel)
            before, after = no_action[(station, fuel)], with_plan[(station, fuel)]
            lp_total = sum(o.quantity for o in group)
            conf = confidence_score(
                age_ticks=snap.max_age(), stale=snap.stale,
                mu=problem.mu[s, f, :lead[station] + 4], sd=problem.sd[s, f, :lead[station] + 4],
                coverage=self.forecaster.coverage(), lp_liters=lp_total,
                heuristic_liters=heur_totals.get((station, fuel), 0.0),
                mape=self.forecaster.mape(station, fuel),
            )
            metrics.DECISION_CONFIDENCE.observe(conf.value)
            results = [self.gate.evaluate(GateInput(o, mode, self.autonomy, conf.value, before.p_stockout,
                                                    before.time_to_stockout_ticks, lead[station], fallback))
                       for o in group]
            result = "review" if any(r.result == "review" for r in results) else "auto"
            reasons = sorted({reason for r in results for reason in r.reasons})
            target = problem.t0 + problem.lag
            keys = [idempotency_key(snap.epoch, o.station_id, o.fuel, o.route_id, target, o.quantity, o.part)
                    for o in group]
            decision_id = f"dec-{self._next_counter('decision_seq'):06d}"
            record = build_record(
                decision_id=decision_id, cycle_id=cycle_id, snapshot=snap, problem=problem, mode=mode,
                policy=f"{plan.policy}-{self.s.policy.get('version', 'v1')}",
                config_version=str(self.s.policy.get("version", "v1")), station=station, fuel=fuel,
                orders=group, keys=keys, no_action=before, with_plan=after,
                alternatives=self._alternatives(problem, group, orders, plan, before),
                binding=plan.binding.get((station, fuel), []), confidence=conf,
                gate={"result": result, "reasons": reasons},
                active_events=[{"id": e.id, "type": e.type, "multiplier": e.parameters.get("multiplier")}
                               for e in mm.active_events(station)],
            )
            metrics.DECISIONS.labels(result).inc()
            if result == "auto":
                for order, key in zip(group, keys):
                    submissions.append(Submission(order, key, decision_id, target))
            else:
                merged = PlannedOrder(route, group[0].depot_id, station, fuel, lp_total, target,
                                      cross_region=group[0].cross_region, critical=group[0].critical)
                rec = self.queue.propose(merged, decision_id, snap.tick, reasons, conf.value, record["impact"])
                record["recommendation_id"] = rec.id
                reviewed.add((station, fuel, route))
            self._store_decision(record)
        self._persist_recommendations()
        return submissions, reviewed

    def _alternatives(self, problem: Problem, group: list[PlannedOrder], all_orders: list[PlannedOrder],
                      plan: PlanResult, before: ProjectionResult) -> list[dict[str, Any]]:
        """Two cheap what-ifs per decision: wait one review period, or use the other route."""
        station, fuel = group[0].station_id, group[0].fuel
        others = [o for o in all_orders if o not in group]
        review = int(self.s.p("planner", "review_ticks", 8))
        alts: list[dict[str, Any]] = []
        delayed = [PlannedOrder(o.route_id, o.depot_id, o.station_id, o.fuel, o.quantity,
                                o.create_tick + review, o.part) for o in group]
        x_wait = orders_to_x(problem, others + [o for o in delayed if o.create_tick - problem.t0 < problem.H])
        window = getattr(self, "_window", problem.H)
        wait = project(problem, plan_arrivals(problem, x_wait), samples=64, horizon=window)[(station, fuel)]
        alts.append({"desc": f"wait {review} ticks", "p_stockout": round(wait.p_stockout, 3),
                     "unmet_l": round(wait.expected_unmet, 1)})
        for r, route in enumerate(problem.routes):
            if (route.destination_station_id != station or route.id == group[0].route_id
                    or not problem.route_ok[r, problem.lag]):
                continue
            moved = [PlannedOrder(route.id, route.source_depot_id, station, fuel,
                                  min(o.quantity, route.max_shipment), o.create_tick, o.part,
                                  bool(problem.cross[r])) for o in group]
            alt = project(problem, plan_arrivals(problem, orders_to_x(problem, others + moved)),
                          samples=64, horizon=window)[(station, fuel)]
            alts.append({"desc": f"use {route.id.replace('route-', '')} instead",
                         "p_stockout": round(alt.p_stockout, 3), "unmet_l": round(alt.expected_unmet, 1)})
        alts.append({"desc": "do nothing", "p_stockout": round(before.p_stockout, 3),
                     "unmet_l": round(before.expected_unmet, 1)})
        return alts

    def _approved_submissions(self, problem: Problem, orders: list[PlannedOrder],
                              snap: Snapshot) -> list[Submission]:
        """Send approved recommendations, after checking them against the latest plan."""
        out: list[Submission] = []
        current = {}
        for order in orders:
            key = (order.station_id, order.fuel, order.route_id)
            current[key] = current.get(key, 0.0) + order.quantity
        headroom = problem.headroom_now()
        for rec in self.queue.approved():
            key = (rec.station_id, rec.fuel, rec.route_id)
            new_qty = current.get(key, 0.0)
            s, f = problem.stations.index(rec.station_id), problem.fuels.index(rec.fuel)
            r = next(i for i, route in enumerate(problem.routes) if route.id == rec.route_id)
            if not problem.route_ok[r, problem.lag] or headroom[s, f] < rec.quantity:
                self.queue.mark(rec, "superseded", "the network changed before it could be sent")
                continue
            if self.queue.needs_requantify(rec, new_qty) and new_qty > 0:
                self.queue.mark(rec, "superseded", f"plan now needs {new_qty:,.0f} L; sent back for review")
                continue
            target = problem.t0 + problem.lag
            route = problem.routes[r]
            remaining, part = rec.quantity, 0
            while remaining > 0:
                qty = min(remaining, (route.max_shipment // 250) * 250)
                order = PlannedOrder(rec.route_id, rec.depot_id, rec.station_id, rec.fuel, qty, target, part,
                                     rec.cross_region)
                key_ = idempotency_key(snap.epoch, rec.station_id, rec.fuel, rec.route_id, target, qty, part)
                out.append(Submission(order, key_, rec.decision_id, target))
                remaining -= qty
                part += 1
            self.queue.mark(rec, "executed", f"approved by {rec.reviewer} and sent at tick {snap.tick}")
        self._persist_recommendations()
        return out

    # --- bookkeeping -------------------------------------------------------
    def _update_mode(self, snap: Snapshot | None) -> None:
        age = snap.max_age() if snap else 10**6
        cover = self._min_cover(snap) if snap else 96.0
        tick = snap.tick if snap else max(self.last_cycle_tick, 0)
        transition = self.modes.update(self.health, min(age, 10**6), cover, tick)
        if transition:
            self._record_transition(transition)

    def _min_cover(self, snap: Snapshot) -> float:
        covers = []
        for station in snap.stations.values():
            region = snap.regions.get(station.region_id)
            rf = self.world.region_factor(station.region_id, region.demand_factor if region else None)
            for fuel in FUELS:
                per_tick = self.world.daily(station.demand_profile, fuel) / self.world.ticks_per_day * rf
                if per_tick > 0:
                    covers.append(station.inventory.get(fuel, 0.0) / (per_tick * station.demand_multiplier))
        return min(covers) if covers else 96.0

    def _record_transition(self, transition: Transition) -> None:
        log.warning("mode.changed", from_mode=transition.from_mode, to_mode=transition.to_mode,
                    reason=transition.reason)
        self.recent_transitions.append(transition.to_dict())
        self.intents.outbox_put("mode_transition", transition.to_dict())

    def _raise_exec_alerts(self, tick: int) -> None:
        for severity, code, message in self._pending_exec_alerts:
            self.alerts.raise_("allocation_error", code, None, severity, message, tick)
        self._pending_exec_alerts = []

    def _store_decision(self, record: dict[str, Any]) -> None:
        self.recent_decisions.append(record)
        self.intents.outbox_put("decision", record)
        log.info("decision.made", decision_id=record["decision_id"], station=record["station"],
                 fuel=record["fuel"], gate=record["gate"]["result"], quantity=record["action"]["quantity_l"])

    def _record_outcome(self, key: str, state: str, allocation_id: int | None, code: str | None = None) -> None:
        intent = self.intents.get(key)
        if intent is None or intent.decision_id is None:
            return
        for record in self.recent_decisions:
            if record["decision_id"] != intent.decision_id:
                continue
            shipments = record["outcome"].setdefault("shipments", {})
            shipments[key] = {"state": state, "allocation_id": allocation_id, "code": code}
            states = {v["state"] for v in shipments.values()}
            record["outcome"]["status"] = ("CONFIRMED" if states == {"CONFIRMED"} else
                                           "REJECTED" if states == {"REJECTED"} else "/".join(sorted(states)))
            record["explanation"] = explain(record)
            self.intents.outbox_put("decision_outcome", {"decision_id": record["decision_id"],
                                                         "outcome": record["outcome"]})
            break

    def _persist_recommendations(self) -> None:
        for rec in self.queue.take_changed():
            payload = rec.to_dict()
            self.intents.save_recommendation(rec.id, rec.state, payload)
            self.intents.outbox_put("recommendation", payload)

    def _persist(self, snap: Snapshot, problem: Problem, cycle_id: str, rows: list) -> None:
        for alert in self.alerts.changed:
            self.intents.outbox_put("alert", alert.to_dict())
        for incident in self.alerts.changed_incidents:
            self.intents.outbox_put("incident", incident.to_dict())
        if rows:
            self.intents.outbox_put("demand_obs", {"rows": [{**r.model_dump(), "epoch": snap.epoch} for r in rows]})
        in_incident = self.alerts.incident is not None
        if snap.tick % 8 == 0 or in_incident:
            forecast_rows = []
            for s, sid in enumerate(problem.stations):
                for f, fuel in enumerate(problem.fuels):
                    for h in (1, 4, 8, 16):
                        if h <= problem.H:
                            mu, sd = float(problem.mu[s, f, h - 1]), float(problem.sd[s, f, h - 1])
                            forecast_rows.append({"tick": snap.tick + h, "station": sid, "fuel": fuel,
                                                  "horizon": h, "p10": max(0.0, mu - 1.2816 * sd),
                                                  "p50": mu, "p90": mu + 1.2816 * sd})
            self.intents.outbox_put("forecast", {"rows": forecast_rows})
        if snap.tick % 16 == 0 or in_incident or any(r["cycle_id"] == cycle_id for r in self.recent_decisions):
            self.intents.outbox_put("snapshot", {"tick": snap.tick, "cycle_id": cycle_id, "resource": "problem",
                                                 "data": problem_to_dict(problem), "stale": snap.stale})
        if snap.tick % 16 == 0:
            self.intents.save_model_state(self.forecaster.dump_state(), snap.tick)

    def _publish(self, snap: Snapshot, problem: Problem, no_action: dict) -> None:
        for station in snap.stations.values():
            for fuel, liters in station.inventory.items():
                metrics.STATION_INVENTORY.labels(station.id, fuel).set(liters)
        for depot in snap.depots.values():
            for fuel, liters in depot.inventory.items():
                metrics.DEPOT_INVENTORY.labels(depot.id, fuel).set(liters)
        for (sid, fuel), res in no_action.items():
            metrics.STOCKOUT_PROB.labels(sid, fuel).set(res.p_stockout)

    # --- read models for the API --------------------------------------------
    def status(self, sse_healthy: bool | None = None) -> dict[str, Any]:
        snap = self.view.snapshot
        now = time.time()
        engine_age = now - self.last_cycle_wall if self.last_cycle_wall else None
        sim_state = "healthy"
        if self.health.error_ratio() > 0.3 or self.health.seconds_since_success() > 10:
            sim_state = "degraded" if self.health.seconds_since_success() < 10 else "down"
        return {
            "boot_id": self.boot_id,
            "mode": self.modes.mode,
            "autonomy": self.autonomy,
            "planner_policy": self.policy_name,
            "policy_version": self.s.policy.get("version"),
            "tick": self.cache.true_tick,
            "last_cycle_tick": self.last_cycle_tick,
            "last_cycle_age_s": round(engine_age, 2) if engine_age is not None else None,
            "cycles": self.cycles,
            "cycle_ms": round(self.view.duration_s * 1000, 1),
            "land_lag_ticks": self.view.problem.lag if self.view.problem else 0,
            "data_age_ticks": snap.max_age() if snap else None,
            "stale": snap.stale if snap else None,
            "simulator_status": snap.status if snap else None,
            "epoch": self.sync.epoch,
            "components": {
                "fuel_simulator": sim_state,
                "prediction_service": "healthy" if self.forecaster_ok else "fallback",
                "decision_engine": "healthy" if self.planner_ok else "fallback",
                "sse": None if sse_healthy is None else ("connected" if sse_healthy else "disconnected"),
            },
            "error_ratio": round(self.health.error_ratio(), 3),
            "sim_p95_ms": round(self.client.latency.quantile(0.95) * 1000, 1),
            "intents": self.intents.counts(),
            "outbox_depth": self.intents.outbox_depth(),
            "pending_recommendations": len(self.queue.pending()),
        }

    def state(self) -> dict[str, Any]:
        """Everything the operator console shows, in one payload."""
        v = self.view
        snap = v.snapshot
        out: dict[str, Any] = {"status": self.status(), "cycle_id": v.cycle_id, "tick": v.tick}
        if snap is None or v.problem is None:
            return out
        p = v.problem
        stations = []
        for s, sid in enumerate(p.stations):
            st = snap.stations[sid]
            fuels = {}
            for f, fuel in enumerate(p.fuels):
                mean_per_tick = float(np.mean(p.mu[s, f, :16])) or 1e-6
                na, wp = v.no_action[(sid, fuel)], v.with_plan[(sid, fuel)]
                outlook = v.outlook.get((sid, fuel), na)
                fuels[fuel] = {
                    "inventory": st.inventory.get(fuel, 0.0), "capacity": st.capacity.get(fuel, 0.0),
                    "in_transit": round(float(p.in_transit_total[s, f]), 1),
                    "cover_hours": round(st.inventory.get(fuel, 0.0) / mean_per_tick * snap.tick_minutes / 60, 1),
                    "forecast": {"p10": np.maximum(0, p.mu[s, f] - 1.2816 * p.sd[s, f]).round(1).tolist(),
                                 "p50": p.mu[s, f].round(1).tolist(),
                                 "p90": (p.mu[s, f] + 1.2816 * p.sd[s, f]).round(1).tolist()},
                    "no_action": na.to_dict(), "with_plan": wp.to_dict(),
                    "band_p10": outlook.band_p10, "band_p90": outlook.band_p90,
                    "mape": round(self.forecaster.mape(sid, fuel), 3),
                }
            stations.append({"id": sid, "name": st.name, "region_id": st.region_id, "status": st.status,
                             "profile": st.demand_profile, "demand_multiplier": st.demand_multiplier,
                             "route_redundancy": int(p.available_routes[s]), "fuels": fuels})
        metrics_obj = self.cache.get("metrics")
        out.update({
            "sim_time": self.cache.get("instance").sim_time if self.cache.get("instance") else None,
            "tick_minutes": snap.tick_minutes,
            "stations": stations,
            "depots": [d.model_dump() for d in snap.depots.values()],
            "routes": [{**r.model_dump(), "cross_region": snap.is_cross_region(r)} for r in snap.routes.values()],
            "regions": [r.model_dump() for r in snap.regions.values()],
            "events": [e.model_dump() for e in snap.events],
            "arrivals": [a.model_dump() for a in snap.arrivals],
            "allocations": [a.model_dump() for a in snap.allocations[:100]],
            "runways": [r.to_dict() for r in self.detector.runways],
            "alerts": [a.to_dict() for a in self.alerts.all_alerts()[:200]],
            "incident": self.alerts.incident.to_dict() if self.alerts.incident else None,
            "incidents": [i.to_dict() for i in self.alerts.incidents[-20:]],
            "recommendations": [r.to_dict() for r in sorted(self.queue.items.values(),
                                                            key=lambda r: r.created_tick, reverse=True)[:50]],
            "decisions": list(self.recent_decisions)[-50:][::-1],
            "mode_transitions": list(self.recent_transitions)[::-1],
            "metrics": metrics_obj.model_dump() if metrics_obj else None,
            "policy_used": v.policy_used,
            "fallback": v.fallback,
        })
        return out
