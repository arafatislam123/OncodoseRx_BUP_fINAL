"""Durable agent state in SQLite (design sections 9.2 and 15).

Every order is written here, and fsync'd, before the POST goes out. After a
crash the agent reloads SENDING and UNKNOWN intents and settles them against
/v1/allocations before planning anything new.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    key TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    state TEXT NOT NULL,
    allocation_id INTEGER,
    target_tick INTEGER NOT NULL,
    decision_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_wall REAL NOT NULL,
    updated_wall REAL NOT NULL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS intents_state ON intents(state);
CREATE TABLE IF NOT EXISTS model_state (
    station TEXT NOT NULL, fuel TEXT NOT NULL, bucket INTEGER NOT NULL,
    mu REAL NOT NULL, var REAL NOT NULL, n INTEGER NOT NULL, updated_tick INTEGER,
    PRIMARY KEY (station, fuel, bucket)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_wall REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_wall REAL NOT NULL
);
"""

OPEN_STATES = ("PLANNED", "SENDING", "UNKNOWN")


@dataclass
class Intent:
    key: str
    epoch: int
    body: dict[str, Any]
    state: str
    target_tick: int
    decision_id: str | None = None
    allocation_id: int | None = None
    attempts: int = 0
    created_wall: float = 0.0
    updated_wall: float = 0.0
    last_error: str | None = None


class IntentLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, tuple(args))

    # --- intents ---------------------------------------------------------
    def add(self, intent: Intent) -> bool:
        """Insert a new intent. Returns False if the key is already known (a replan of the same order)."""
        now = time.time()
        try:
            self._exec(
                "INSERT INTO intents (key, epoch, body_json, state, target_tick, decision_id, attempts,"
                " created_wall, updated_wall) VALUES (?,?,?,?,?,?,?,?,?)",
                (intent.key, intent.epoch, json.dumps(intent.body), intent.state, intent.target_tick,
                 intent.decision_id, 0, now, now),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_state(self, key: str, state: str, allocation_id: int | None = None,
                  error: str | None = None, attempt: bool = False) -> None:
        self._exec(
            "UPDATE intents SET state=?, allocation_id=COALESCE(?, allocation_id), last_error=?,"
            " attempts=attempts + ?, updated_wall=? WHERE key=?",
            (state, allocation_id, error, 1 if attempt else 0, time.time(), key),
        )

    def get(self, key: str) -> Intent | None:
        row = self._exec("SELECT * FROM intents WHERE key=?", (key,)).fetchone()
        return self._row(row) if row else None

    def open_intents(self) -> list[Intent]:
        rows = self._exec(
            f"SELECT * FROM intents WHERE state IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY created_wall",
            OPEN_STATES,
        ).fetchall()
        return [self._row(r) for r in rows]

    def recover_after_crash(self) -> int:
        """Anything that was mid-send when we died might have landed: mark it UNKNOWN."""
        cur = self._exec("UPDATE intents SET state='UNKNOWN', updated_wall=? WHERE state IN ('PLANNED','SENDING')",
                         (time.time(),))
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        rows = self._exec("SELECT state, COUNT(*) FROM intents GROUP BY state").fetchall()
        return {state: n for state, n in rows}

    def recent(self, limit: int = 100) -> list[Intent]:
        rows = self._exec("SELECT * FROM intents ORDER BY created_wall DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: tuple[Any, ...]) -> Intent:
        (key, epoch, body_json, state, allocation_id, target_tick, decision_id, attempts,
         created_wall, updated_wall, last_error) = row
        return Intent(key=key, epoch=epoch, body=json.loads(body_json), state=state, target_tick=target_tick,
                      decision_id=decision_id, allocation_id=allocation_id, attempts=attempts,
                      created_wall=created_wall, updated_wall=updated_wall, last_error=last_error)

    # --- forecaster state ------------------------------------------------
    def save_model_state(self, rows: list[tuple[str, str, int, float, float, int]], tick: int) -> None:
        with self._lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT INTO model_state (station, fuel, bucket, mu, var, n, updated_tick) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(station, fuel, bucket) DO UPDATE SET mu=excluded.mu, var=excluded.var,"
                " n=excluded.n, updated_tick=excluded.updated_tick",
                [(*row, tick) for row in rows],
            )
            self.db.execute("COMMIT")

    def load_model_state(self) -> list[tuple[str, str, int, float, float, int]]:
        return self._exec("SELECT station, fuel, bucket, mu, var, n FROM model_state").fetchall()

    # --- outbox ------------------------------------------------------------
    def outbox_put(self, kind: str, payload: dict[str, Any]) -> None:
        self._exec("INSERT INTO outbox (kind, payload_json, created_wall) VALUES (?,?,?)",
                   (kind, json.dumps(payload, default=str), time.time()))

    def outbox_peek(self, limit: int = 200) -> list[tuple[int, str, dict[str, Any]]]:
        rows = self._exec("SELECT id, kind, payload_json FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(i, kind, json.loads(p)) for i, kind, p in rows]

    def outbox_delete(self, ids: list[int]) -> None:
        if ids:
            self._exec(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids)

    def outbox_depth(self) -> int:
        return self._exec("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # --- recommendations -------------------------------------------------
    def save_recommendation(self, rec_id: str, state: str, payload: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO recommendations (id, state, payload_json, updated_wall) VALUES (?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET state=excluded.state, payload_json=excluded.payload_json,"
            " updated_wall=excluded.updated_wall",
            (rec_id, state, json.dumps(payload, default=str), time.time()),
        )

    def pending_recommendations(self) -> list[dict[str, Any]]:
        rows = self._exec("SELECT payload_json FROM recommendations WHERE state IN ('pending','approved')").fetchall()
        return [json.loads(r[0]) for r in rows]

    # --- small key/value store ---------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._exec("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))
