"""Alert bookkeeping and incident grouping (design section 8.3 and 13).

Detectors call raise_() every cycle for each condition that currently holds.
Alerts are grouped by (type, entity, fuel); anything not raised again in a
cycle gets resolved. Two or more warnings open at once form an incident.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from typing import Any

from agent import metrics

SEVERITY_RANK = {"info": 0, "warn": 1, "crit": 2}


@dataclass
class Alert:
    id: str
    type: str
    severity: str
    entity: str
    fuel: str | None
    message: str
    evidence: dict[str, Any]
    first_seen_tick: int
    last_seen_tick: int
    state: str = "open"
    decision_ids: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.type, self.entity, self.fuel)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    id: str
    opened_tick: int
    alert_ids: list[str]
    closed_tick: int | None = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertManager:
    def __init__(self, incident_quiet_ticks: int = 4, id_prefix: str = "") -> None:
        # ids are written to Postgres, so a per-boot prefix keeps them unique across restarts
        self.prefix = id_prefix
        self._ids = itertools.count(1)
        self._incident_ids = itertools.count(1)
        self.open: dict[tuple[str, str, str | None], Alert] = {}
        self.recent: list[Alert] = []          # resolved alerts, newest last
        self.changed: list[Alert] = []         # alerts to write to Postgres this cycle
        self._seen: set[tuple[str, str, str | None]] = set()
        self.incident: Incident | None = None
        self.incidents: list[Incident] = []
        self.changed_incidents: list[Incident] = []
        self._quiet_since: int | None = None
        self.incident_quiet_ticks = incident_quiet_ticks

    def begin_cycle(self) -> None:
        self._seen = set()
        self.changed = []
        self.changed_incidents = []

    def raise_(self, type_: str, entity: str, fuel: str | None, severity: str, message: str,
               tick: int, evidence: dict[str, Any] | None = None) -> Alert:
        key = (type_, entity, fuel)
        self._seen.add(key)
        alert = self.open.get(key)
        if alert is None:
            alert = Alert(f"al-{self.prefix}{next(self._ids)}", type_, severity, entity, fuel, message,
                          evidence or {}, tick, tick)
            self.open[key] = alert
            metrics.ALERTS_RAISED.labels(type_, severity).inc()
            self.changed.append(alert)
            return alert
        escalated = SEVERITY_RANK[severity] > SEVERITY_RANK[alert.severity]
        if escalated:
            metrics.ALERTS_RAISED.labels(type_, severity).inc()
        if escalated or severity != alert.severity or message != alert.message:
            self.changed.append(alert)
        alert.severity, alert.message, alert.last_seen_tick = severity, message, tick
        alert.evidence = evidence or alert.evidence
        return alert

    def is_open(self, type_: str, entity: str, fuel: str | None) -> Alert | None:
        return self.open.get((type_, entity, fuel))

    def end_cycle(self, tick: int, keep_types: tuple[str, ...] = ()) -> None:
        """Resolve alerts that weren't raised this cycle, then update incidents."""
        for key in list(self.open):
            if key in self._seen or key[0] in keep_types:
                continue
            alert = self.open.pop(key)
            alert.state = "resolved"
            alert.last_seen_tick = tick
            self.recent.append(alert)
            self.changed.append(alert)
        self.recent = self.recent[-500:]
        self._update_incident(tick)

    def _update_incident(self, tick: int) -> None:
        serious = [a for a in self.open.values() if SEVERITY_RANK[a.severity] >= 1]
        if self.incident is None:
            if len(serious) >= 2:
                self.incident = Incident(f"inc-{self.prefix}{next(self._incident_ids)}", tick,
                                         [a.id for a in serious])
                self.incident.summary = self.summarise(serious)
                self.incidents.append(self.incident)
                self.changed_incidents.append(self.incident)
                self._quiet_since = None
            return
        if serious:
            new_ids = [a.id for a in serious if a.id not in self.incident.alert_ids]
            if new_ids:
                self.incident.alert_ids.extend(new_ids)
                self.incident.summary = self.summarise(serious)
                self.changed_incidents.append(self.incident)
            self._quiet_since = None
            return
        if self._quiet_since is None:
            self._quiet_since = tick
        elif tick - self._quiet_since >= self.incident_quiet_ticks:
            self.incident.closed_tick = tick
            self.incident.summary += f" Closed at tick {tick} after {tick - self.incident.opened_tick} ticks."
            self.changed_incidents.append(self.incident)
            self.incident = None
            self._quiet_since = None
        self.incidents = self.incidents[-100:]

    @staticmethod
    def summarise(alerts: list[Alert]) -> str:
        crit = [a for a in alerts if a.severity == "crit"]
        parts = [f"{len(alerts)} open warnings ({len(crit)} critical)."]
        for alert in sorted(alerts, key=lambda a: -SEVERITY_RANK[a.severity])[:5]:
            parts.append(alert.message)
        return " ".join(parts)

    def all_alerts(self) -> list[Alert]:
        return list(self.open.values()) + list(reversed(self.recent))
