"""The one place that applies demand_spike multipliers (design section 8.2).

The live station.demand_multiplier already includes every ACTIVE spike, so a
naive forecaster that also multiplies by the event would count it twice. Here
each spike is tracked by its event id and applied exactly once for any tick,
past or future.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.sync.models import Event, Station
from agent.sync.snapshot import Snapshot


def event_matches(event: Event, station: Station) -> bool:
    """Empty filter lists mean "all", as the guide says. Non-empty filters must all match."""
    params = event.parameters or {}
    station_ids = params.get("station_ids") or []
    region_ids = params.get("region_ids") or []
    if station_ids and station.id not in station_ids:
        return False
    if region_ids and station.region_id not in region_ids:
        return False
    return True


def spike_multiplier(event: Event) -> float:
    return max(0.01, float((event.parameters or {}).get("multiplier", 1.5)))


@dataclass
class _StationSpikes:
    live: float
    active: list[Event] = field(default_factory=list)
    scheduled: list[Event] = field(default_factory=list)
    resolved: list[Event] = field(default_factory=list)


class MultiplierModel:
    def __init__(self, snapshot: Snapshot, baseline: float = 1.0, tolerance: float = 1e-3) -> None:
        self.baseline = baseline
        self.consistent = True
        self._stations: dict[str, _StationSpikes] = {}
        spikes = [e for e in snapshot.events if e.type == "demand_spike"]

        for station in snapshot.stations.values():
            info = _StationSpikes(live=station.demand_multiplier)
            for event in spikes:
                if not event_matches(event, station):
                    continue
                {"ACTIVE": info.active, "SCHEDULED": info.scheduled,
                 "RESOLVED": info.resolved}[event.status].append(event)

            expected = baseline
            for event in info.active:
                expected *= spike_multiplier(event)
            if abs(info.live - expected) > tolerance * max(expected, 1e-6):
                # the events and stations reads straddle a status change; work the
                # active set out from ticks instead and trust the station value
                self.consistent = False
                info = self._rederive(info, snapshot.tick)
            self._stations[station.id] = info

    @staticmethod
    def _rederive(info: _StationSpikes, now: int) -> _StationSpikes:
        everything = info.active + info.scheduled + info.resolved
        fixed = _StationSpikes(live=info.live)
        for event in everything:
            if event.start_tick <= now < event.end_tick:
                fixed.active.append(event)
            elif event.start_tick > now:
                fixed.scheduled.append(event)
            else:
                fixed.resolved.append(event)
        return fixed

    def m(self, station_id: str, tick: int) -> float:
        """Demand multiplier for a station at any tick."""
        info = self._stations.get(station_id)
        if info is None:
            return self.baseline
        value = info.live
        for event in info.active:
            if not (event.start_tick <= tick < event.end_tick):
                value /= spike_multiplier(event)
        for event in info.scheduled + info.resolved:
            if event.start_tick <= tick < event.end_tick:
                value *= spike_multiplier(event)
        return value

    def near_transition(self, station_id: str, tick: int, margin: int = 1) -> bool:
        """True around the start or end of a spike, where timing is ambiguous."""
        info = self._stations.get(station_id)
        if info is None:
            return False
        for event in info.active + info.scheduled + info.resolved:
            if abs(tick - event.start_tick) <= margin or abs(tick - event.end_tick) <= margin:
                return True
        return False

    def active_events(self, station_id: str) -> list[Event]:
        info = self._stations.get(station_id)
        return list(info.active) if info else []
