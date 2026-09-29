"""Decision gate and approval queue (design sections 10 and 11.2).

The gate decides, per proposed order, whether to send it now ("auto") or hand
it to a person ("review"). Queued recommendations expire after a few ticks so
an approval never acts on an old picture of the world.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent.intelligence.orders import PlannedOrder


@dataclass
class GateResult:
    result: str                 # auto | review
    reasons: list[str] = field(default_factory=list)


@dataclass
class GateInput:
    order: PlannedOrder
    mode: str
    autonomy: str
    confidence: float
    p_stockout_before: float
    time_to_stockout: int | None
    lead_ticks: int
    fallback: bool


class Gate:
    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        p = policy or {}
        self.threshold = float(p.get("confidence_review_threshold", 0.6))
        self.large_order = float(p.get("large_order_liters", 5000))
        self.degraded_min_p = float(p.get("degraded_auto_min_p_stockout", 0.3))

    def evaluate(self, g: GateInput) -> GateResult:
        emergency = g.time_to_stockout is not None and g.time_to_stockout <= g.lead_ticks + 2
        if g.mode == "MANUAL" or g.autonomy == "manual":
            return GateResult("review", ["manual mode: every order needs approval"])
        if g.mode == "SAFE":
            if emergency:
                return GateResult("auto", ["SAFE mode, emergency order"])
            return GateResult("review", ["SAFE mode: only emergency orders are sent"])
        if g.mode == "DEGRADED":
            if g.p_stockout_before >= self.degraded_min_p:
                return GateResult("auto", [f"DEGRADED mode, stockout risk {g.p_stockout_before:.0%}"])
            return GateResult("review", ["DEGRADED mode: low-risk orders wait for review"])

        flags: list[str] = []
        if g.confidence < self.threshold:
            flags.append(f"confidence {g.confidence:.2f} below {self.threshold}")
        if g.order.cross_region:
            flags.append("cross-region shipment")
        if g.order.quantity > self.large_order:
            flags.append(f"order above {self.large_order:,.0f} L")
        if g.fallback:
            flags.append("planned by the fallback policy")
        if g.autonomy == "autopilot" or not flags:
            return GateResult("auto", flags)
        if emergency and g.autonomy == "copilot" and g.confidence >= 0.3:
            return GateResult("auto", flags + ["sent anyway: stockout is imminent"])
        return GateResult("review", flags)


@dataclass
class Recommendation:
    id: str
    decision_id: str
    station_id: str
    fuel: str
    route_id: str
    depot_id: str
    quantity: float
    created_tick: int
    expires_tick: int
    state: str = "pending"      # pending | approved | rejected | expired | executed | superseded
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0
    impact: dict[str, Any] = field(default_factory=dict)
    cross_region: bool = False
    reviewer: str | None = None
    reviewed_at: float | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalQueue:
    def __init__(self, ttl_ticks: int = 8, tolerance: float = 0.10,
                 next_id: Callable[[], str] | None = None) -> None:
        self.ttl = ttl_ticks
        self.tolerance = tolerance
        self.items: dict[str, Recommendation] = {}
        counter = itertools.count(1)
        self._next_id = next_id or (lambda: f"rec-{next(counter):06d}")
        self.changed: list[Recommendation] = []

    def restore(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            rec = Recommendation(**row)
            self.items[rec.id] = rec

    def _key(self, station: str, fuel: str, route: str) -> tuple[str, str, str]:
        return (station, fuel, route)

    def find(self, station: str, fuel: str, route: str) -> Recommendation | None:
        for rec in self.items.values():
            if rec.state in ("pending", "approved") and self._key(rec.station_id, rec.fuel, rec.route_id) == \
                    self._key(station, fuel, route):
                return rec
        return None

    def propose(self, order: PlannedOrder, decision_id: str, tick: int, reasons: list[str],
                confidence: float, impact: dict[str, Any]) -> Recommendation:
        """Add a recommendation, or refresh the pending one for the same station, fuel and route."""
        rec = self.find(order.station_id, order.fuel, order.route_id)
        if rec is not None and rec.state == "pending":
            rec.quantity, rec.decision_id, rec.reasons = order.quantity, decision_id, reasons
            rec.confidence, rec.impact = confidence, impact
            self.changed.append(rec)
            return rec
        if rec is not None:
            return rec   # approved and waiting to be sent
        rec = Recommendation(
            id=self._next_id(), decision_id=decision_id, station_id=order.station_id,
            fuel=order.fuel, route_id=order.route_id, depot_id=order.depot_id, quantity=order.quantity,
            created_tick=tick, expires_tick=tick + self.ttl, reasons=reasons, confidence=confidence,
            impact=impact, cross_region=order.cross_region,
        )
        self.items[rec.id] = rec
        self.changed.append(rec)
        return rec

    def review(self, rec_id: str, approve: bool, reviewer: str, note: str | None = None) -> Recommendation | None:
        rec = self.items.get(rec_id)
        if rec is None or rec.state != "pending":
            return None
        rec.state = "approved" if approve else "rejected"
        rec.reviewer, rec.reviewed_at, rec.note = reviewer, time.time(), note
        self.changed.append(rec)
        return rec

    def expire(self, tick: int, still_wanted: set[tuple[str, str, str]]) -> None:
        """Expire old ones, and withdraw pending ones the latest plan no longer asks for."""
        for rec in self.items.values():
            if rec.state != "pending":
                continue
            if tick > rec.expires_tick:
                rec.state, rec.note = "expired", "expired before review; will be re-planned"
                self.changed.append(rec)
            elif self._key(rec.station_id, rec.fuel, rec.route_id) not in still_wanted:
                rec.state, rec.note = "expired", "withdrawn: the latest plan no longer needs it"
                self.changed.append(rec)
        # keep memory bounded
        done = [r for r in self.items.values() if r.state not in ("pending", "approved")]
        for rec in sorted(done, key=lambda r: r.created_tick)[:-300]:
            del self.items[rec.id]

    def approved(self) -> list[Recommendation]:
        return [r for r in self.items.values() if r.state == "approved"]

    def mark(self, rec: Recommendation, state: str, note: str | None = None) -> None:
        rec.state = state
        if note:
            rec.note = note
        self.changed.append(rec)

    def needs_requantify(self, rec: Recommendation, new_quantity: float) -> bool:
        if rec.quantity <= 0:
            return True
        return abs(new_quantity - rec.quantity) / rec.quantity > self.tolerance

    def take_changed(self) -> list[Recommendation]:
        changed, self.changed = self.changed, []
        unique = {r.id: r for r in changed}
        return list(unique.values())

    def pending(self) -> list[Recommendation]:
        return sorted((r for r in self.items.values() if r.state == "pending"), key=lambda r: r.created_tick)
