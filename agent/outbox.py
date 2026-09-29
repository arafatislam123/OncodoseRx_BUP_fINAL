"""Moves records from the SQLite outbox to Postgres.

The agent never waits on Postgres. Records pile up in SQLite while Postgres is
down and get written once it's back (design section 3, failure boundaries).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog

from agent import metrics
from agent.execution.intent_log import IntentLog

log = structlog.get_logger()

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
MAX_LOCAL_BACKLOG = 50_000

UPSERTS: dict[str, str] = {
    "decision": """
        INSERT INTO decisions (id, cycle_id, tick, station, fuel, gate, confidence, record, explanation, outcome)
        VALUES (%(decision_id)s, %(cycle_id)s, %(tick)s, %(station)s, %(fuel)s, %(gate_result)s,
                %(confidence_value)s, %(record)s, %(explanation)s, %(outcome)s)
        ON CONFLICT (id) DO UPDATE SET record = EXCLUDED.record, gate = EXCLUDED.gate,
            explanation = EXCLUDED.explanation, outcome = EXCLUDED.outcome""",
    "decision_outcome": """
        UPDATE decisions SET outcome = %(outcome)s,
            record = jsonb_set(record, '{outcome}', %(outcome)s) WHERE id = %(decision_id)s""",
    "alert": """
        INSERT INTO alerts (id, type, severity, entity, fuel, state, message, first_seen_tick, last_seen_tick,
                            evidence, updated_at)
        VALUES (%(id)s, %(type)s, %(severity)s, %(entity)s, %(fuel)s, %(state)s, %(message)s,
                %(first_seen_tick)s, %(last_seen_tick)s, %(evidence)s, now())
        ON CONFLICT (id) DO UPDATE SET severity = EXCLUDED.severity, state = EXCLUDED.state,
            message = EXCLUDED.message, last_seen_tick = EXCLUDED.last_seen_tick,
            evidence = EXCLUDED.evidence, updated_at = now()""",
    "incident": """
        INSERT INTO incidents (id, opened_tick, closed_tick, alert_ids, summary, updated_at)
        VALUES (%(id)s, %(opened_tick)s, %(closed_tick)s, %(alert_ids)s, %(summary)s, now())
        ON CONFLICT (id) DO UPDATE SET closed_tick = EXCLUDED.closed_tick, alert_ids = EXCLUDED.alert_ids,
            summary = EXCLUDED.summary, updated_at = now()""",
    "recommendation": """
        INSERT INTO recommendations (id, decision_id, state, station, fuel, quantity, expires_tick, payload,
                                     reviewer, reviewed_at, updated_at)
        VALUES (%(id)s, %(decision_id)s, %(state)s, %(station_id)s, %(fuel)s, %(quantity)s, %(expires_tick)s,
                %(payload)s, %(reviewer)s, to_timestamp(%(reviewed_at)s), now())
        ON CONFLICT (id) DO UPDATE SET decision_id = EXCLUDED.decision_id, state = EXCLUDED.state,
            quantity = EXCLUDED.quantity, payload = EXCLUDED.payload, reviewer = EXCLUDED.reviewer,
            reviewed_at = EXCLUDED.reviewed_at, updated_at = now()""",
    "mode_transition": """
        INSERT INTO mode_transitions (from_mode, to_mode, reason, wall, tick)
        VALUES (%(from)s, %(to)s, %(reason)s, to_timestamp(%(wall)s), %(tick)s)""",
    "demand_obs": """
        INSERT INTO demand_obs (epoch, id, station, fuel, tick, demand, served, unmet)
        VALUES (%(epoch)s, %(id)s, %(station_id)s, %(fuel_type)s, %(tick)s, %(demand_liters)s,
                %(served_liters)s, %(unmet_liters)s)
        ON CONFLICT DO NOTHING""",
    "forecast": """
        INSERT INTO forecasts (tick, station, fuel, horizon, p10, p50, p90)
        VALUES (%(tick)s, %(station)s, %(fuel)s, %(horizon)s, %(p10)s, %(p50)s, %(p90)s)
        ON CONFLICT DO NOTHING""",
    "snapshot": """
        INSERT INTO snapshots (tick, cycle_id, resource, data, stale)
        VALUES (%(tick)s, %(cycle_id)s, %(resource)s, %(data)s, %(stale)s)""",
}

JSON_FIELDS = {"record", "outcome", "evidence", "alert_ids", "payload", "data"}


def _params(kind: str, payload: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    """Shape a payload into query parameters. JSON columns are sent as text."""
    if kind == "decision":
        payload = {
            **payload,
            "gate_result": payload["gate"]["result"],
            "confidence_value": payload["confidence"]["value"],
            "record": payload,
            "outcome": payload.get("outcome"),
        }
    elif kind == "recommendation":
        payload = {**payload, "payload": payload}
    rows = payload["rows"] if kind in ("demand_obs", "forecast") else [payload]
    out = []
    for row in rows:
        row = dict(row)
        for key in JSON_FIELDS & row.keys():
            if row[key] is not None and not isinstance(row[key], str):
                row[key] = json.dumps(row[key], default=str)
        out.append(row)
    return out


class OutboxWriter:
    def __init__(self, dsn: str, intents: IntentLog) -> None:
        self.dsn = dsn
        self.intents = intents
        self._conn: Any = None
        self.healthy = False

    async def _connect(self) -> Any:
        import psycopg  # imported lazily so the agent runs without it in tests

        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, connect_timeout=3)
            schema = SCHEMA_PATH.read_text(encoding="utf-8")
            async with self._conn.cursor() as cur:
                await cur.execute(schema)
        return self._conn

    async def flush_once(self, batch: int = 200) -> int:
        items = self.intents.outbox_peek(batch)
        metrics.OUTBOX_DEPTH.set(self.intents.outbox_depth())
        if not items:
            return 0
        if not self.dsn:
            # no database configured (tests, local runs): keep a bounded backlog only
            if self.intents.outbox_depth() > MAX_LOCAL_BACKLOG:
                self.intents.outbox_delete([i for i, _, _ in items])
            return 0
        conn = await self._connect()
        done: list[int] = []
        async with conn.cursor() as cur:
            for item_id, kind, payload in items:
                sql = UPSERTS.get(kind)
                if sql is None:
                    done.append(item_id)
                    continue
                try:
                    await cur.executemany(sql, _params(kind, payload))
                    done.append(item_id)
                except Exception as exc:  # a bad row must not block the queue forever
                    if "connection" in str(exc).lower():
                        raise
                    log.warning("outbox.row_failed", kind=kind, error=str(exc)[:300])
                    done.append(item_id)
        self.intents.outbox_delete(done)
        return len(done)

    async def run(self, interval_s: float = 1.0) -> None:
        backoff = interval_s
        while True:
            try:
                while await self.flush_once() > 0:
                    pass
                self.healthy = bool(self.dsn)
                backoff = interval_s
            except Exception as exc:
                self.healthy = False
                self._conn = None
                log.warning("outbox.flush_failed", error=str(exc)[:200])
                backoff = min(backoff * 2, 10.0)
            metrics.OUTBOX_DEPTH.set(self.intents.outbox_depth())
            await asyncio.sleep(backoff)
