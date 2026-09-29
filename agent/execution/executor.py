"""Sends orders and settles their outcome (design sections 9.2 and 9.3).

* The intent is saved before the POST.
* Each depot gets its own lane; lanes run at the same time (one after the other
  in stepped mode, so runs are reproducible).
* No retries inside a cycle. An UNKNOWN intent is retried on a later cycle with
  the same key and body, which is always safe, or abandoned once it's too late.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from agent import metrics
from agent.execution.intent_log import Intent, IntentLog
from agent.intelligence.orders import PlannedOrder
from agent.intelligence.problem import InFlight
from agent.sync.client import SimClient
from agent.sync.models import Allocation, AllocationRequest

log = structlog.get_logger()

RESYNC_CODES = {"NOT_FOUND", "ROUTE_MISMATCH", "DEPOT_CLOSED", "STATION_CLOSED", "ROUTE_DISRUPTED",
                "INSUFFICIENT_INVENTORY", "DESTINATION_CAPACITY_EXCEEDED"}
BUG_CODES = {"ROUTE_MISMATCH", "ROUTE_CAPACITY_EXCEEDED", "NOT_FOUND"}


@dataclass
class Submission:
    order: PlannedOrder
    key: str
    decision_id: str
    target_tick: int


@dataclass
class Outcome:
    key: str
    state: str
    code: str | None = None
    allocation_id: int | None = None
    created_tick: int | None = None


@dataclass
class ExecutorEvents:
    """Things the agent loop has to react to after a round of sends."""

    resync: bool = False
    reconcile: bool = False
    alerts: list[tuple[str, str, str]] = field(default_factory=list)   # (severity, code, message)
    rejected: dict[str, str] = field(default_factory=dict)             # key -> code


class Executor:
    def __init__(self, client: SimClient, intents: IntentLog, sequential: bool = False,
                 settle_s: float = 3.0, retry_grace_ticks: int = 2) -> None:
        self.client = client
        self.intents = intents
        self.sequential = sequential
        self.settle_s = settle_s
        self.retry_grace_ticks = retry_grace_ticks
        self.events = ExecutorEvents()

    def take_events(self) -> ExecutorEvents:
        events, self.events = self.events, ExecutorEvents()
        return events

    @staticmethod
    def body(order: PlannedOrder, key: str) -> AllocationRequest:
        return AllocationRequest(
            idempotency_key=key, source_depot_id=order.depot_id, destination_station_id=order.station_id,
            route_id=order.route_id, fuel_type=order.fuel, quantity=order.quantity,
        )

    async def submit(self, submissions: list[Submission], epoch: int) -> list[Outcome]:
        lanes: dict[str, list[Submission]] = defaultdict(list)
        for sub in submissions:
            request = self.body(sub.order, sub.key)
            fresh = self.intents.add(Intent(key=sub.key, epoch=epoch, body=request.model_dump(),
                                            state="SENDING", target_tick=sub.target_tick,
                                            decision_id=sub.decision_id))
            if not fresh:
                existing = self.intents.get(sub.key)
                if existing and existing.state in ("CONFIRMED", "REJECTED", "ABANDONED"):
                    continue   # same order as last cycle and already settled
            lanes[sub.order.depot_id].append(sub)

        if self.sequential:
            results = [await self._lane(lane) for lane in lanes.values()]
        else:
            results = await asyncio.gather(*(self._lane(lane) for lane in lanes.values()))
        self._publish_counts()
        return [outcome for lane in results for outcome in lane]

    async def _lane(self, lane: list[Submission]) -> list[Outcome]:
        return [await self._send(sub.key, self.body(sub.order, sub.key)) for sub in lane]

    async def _send(self, key: str, request: AllocationRequest) -> Outcome:
        result = await self.client.post_allocation(request)
        if result.outcome == "created":
            alloc_id = (result.body or {}).get("id")
            created = (result.body or {}).get("created_tick")
            self.intents.set_state(key, "CONFIRMED", allocation_id=alloc_id, attempt=True)
            metrics.ALLOCATIONS_SUBMITTED.labels("created").inc()
            log.info("allocation.confirmed", idempotency_key=key, allocation_id=alloc_id)
            return Outcome(key, "CONFIRMED", allocation_id=alloc_id, created_tick=created)

        if result.outcome == "unknown":
            self.intents.set_state(key, "UNKNOWN", error=result.code or "timeout", attempt=True)
            metrics.ALLOCATIONS_SUBMITTED.labels("unknown").inc()
            self.events.reconcile = True
            log.warning("allocation.unknown", idempotency_key=key, status=result.status)
            return Outcome(key, "UNKNOWN", code=result.code)

        code = result.code or f"HTTP_{result.status}"
        self.intents.set_state(key, "REJECTED", error=code, attempt=True)
        metrics.ALLOCATIONS_SUBMITTED.labels("rejected").inc()
        metrics.ALLOCATION_REJECTIONS.labels(code).inc()
        self.events.rejected[key] = code
        log.warning("allocation.rejected", idempotency_key=key, code=code)
        if code in RESYNC_CODES:
            self.events.resync = True
        if code == "IDEMPOTENCY_KEY_MISMATCH" or result.outcome == "client_error":
            self.events.alerts.append(("crit", code, f"Allocation {key} was rejected with {code}. This is a bug."))
        elif code in BUG_CODES:
            self.events.alerts.append(("warn", code, f"Allocation {key} was rejected with {code}."))
        if code in ("DISPATCH_CAPACITY_EXCEEDED", "INSUFFICIENT_INVENTORY", "DESTINATION_CAPACITY_EXCEEDED"):
            self.events.reconcile = True
        return Outcome(key, "REJECTED", code=code)

    async def reconcile(self, allocations: list[Allocation], tick: int) -> list[Outcome]:
        """Settle UNKNOWN intents against the simulator's ledger, retrying the ones still in time."""
        by_key = {a.idempotency_key: a for a in allocations}
        outcomes: list[Outcome] = []
        now = time.time()
        for intent in self.intents.open_intents():
            found = by_key.get(intent.key)
            if found is not None:
                self.intents.set_state(intent.key, "CONFIRMED", allocation_id=found.id)
                outcomes.append(Outcome(intent.key, "CONFIRMED", allocation_id=found.id,
                                        created_tick=found.created_tick))
                continue
            if intent.state != "UNKNOWN":
                continue
            settled = now - intent.updated_wall >= self.settle_s
            if tick <= intent.target_tick + self.retry_grace_ticks:
                # still useful: resend exactly the same key and body
                outcomes.append(await self._send(intent.key, AllocationRequest.model_validate(intent.body)))
            elif settled:
                self.intents.set_state(intent.key, "ABANDONED", error="not found after settle window")
                log.info("allocation.abandoned", idempotency_key=intent.key)
                outcomes.append(Outcome(intent.key, "ABANDONED"))
        self._publish_counts()
        return outcomes

    def in_flight(self, known_keys: set[str]) -> list[InFlight]:
        """Orders the planner must treat as sent but can't see in the cached ledger yet.

        That's UNKNOWN intents (they might have landed) and orders confirmed since
        the last /v1/allocations read. Without the second group the planner would
        order the same fuel again in the gap between two reconciles.
        """
        items = []
        pending = self.intents.open_intents() + [
            i for i in self.intents.recently_confirmed() if i.key not in known_keys
        ]
        for intent in pending:
            body = intent.body
            items.append(InFlight(depot=body["source_depot_id"], station=body["destination_station_id"],
                                  route=body["route_id"], fuel=body["fuel_type"], quantity=body["quantity"],
                                  created_tick=intent.target_tick))
        return items

    def _publish_counts(self) -> None:
        counts = self.intents.counts()
        for state in ("SENDING", "UNKNOWN", "CONFIRMED", "REJECTED", "ABANDONED"):
            metrics.INTENTS.labels(state).set(counts.get(state, 0))
