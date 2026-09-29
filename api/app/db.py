"""Postgres access. Every query degrades to an empty result when the database is down."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from app.config import settings

log = structlog.get_logger()
SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: Any = None

    async def open(self) -> None:
        if not self.dsn:
            return
        from psycopg_pool import AsyncConnectionPool

        self.pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=10, open=False,
                                        kwargs={"autocommit": True, "connect_timeout": 3})
        try:
            await self.pool.open(wait=True, timeout=10)
            if SCHEMA.exists():
                await self.execute(SCHEMA.read_text(encoding="utf-8"))
        except Exception as exc:  # the api must start even when Postgres doesn't
            log.warning("db.unavailable_at_start", error=str(exc)[:200])

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def ping(self) -> bool:
        if self.pool is None:
            return False
        try:
            await self.fetch("SELECT 1", timeout=1.0)
            return True
        except Exception:
            return False

    async def execute(self, sql: str, params: Any = None) -> None:
        async with self.pool.connection(timeout=2) as conn:
            await conn.execute(sql, params)

    async def fetch(self, sql: str, params: Any = None, timeout: float = 2.0) -> list[dict[str, Any]]:
        if self.pool is None:
            raise RuntimeError("database not configured")
        from psycopg.rows import dict_row

        async with self.pool.connection(timeout=timeout) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def try_fetch(self, sql: str, params: Any = None) -> list[dict[str, Any]] | None:
        """None means "database unavailable", as opposed to an empty result."""
        try:
            return await self.fetch(sql, params)
        except Exception as exc:
            log.warning("db.query_failed", error=str(exc)[:200])
            return None

    # --- queries used by the routes ---------------------------------------------
    async def decisions(self, limit: int, station: str | None, fuel: str | None,
                        gate: str | None, before_tick: int | None) -> list[dict[str, Any]] | None:
        clauses, params = [], []
        for column, value in (("station", station), ("fuel", fuel), ("gate", gate)):
            if value:
                clauses.append(f"{column} = %s")
                params.append(value)
        if before_tick is not None:
            clauses.append("tick < %s")
            params.append(before_tick)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self.try_fetch(
            f"SELECT record, explanation, outcome FROM decisions {where} ORDER BY tick DESC, id DESC LIMIT %s",
            (*params, limit),
        )
        if rows is None:
            return None
        return [{**r["record"], "outcome": r["outcome"] or r["record"].get("outcome")} for r in rows]

    async def decision(self, decision_id: str) -> dict[str, Any] | None:
        rows = await self.try_fetch("SELECT record, outcome FROM decisions WHERE id = %s", (decision_id,))
        if not rows:
            return None
        return {**rows[0]["record"], "outcome": rows[0]["outcome"] or rows[0]["record"].get("outcome")}

    async def cycle_problem(self, cycle_id: str) -> dict[str, Any] | None:
        rows = await self.try_fetch(
            "SELECT data FROM snapshots WHERE cycle_id = %s AND resource = 'problem' ORDER BY id DESC LIMIT 1",
            (cycle_id,),
        )
        return rows[0]["data"] if rows else None

    async def alerts(self, state: str | None, limit: int = 200) -> list[dict[str, Any]] | None:
        if state:
            return await self.try_fetch("SELECT * FROM alerts WHERE state = %s ORDER BY updated_at DESC LIMIT %s",
                                        (state, limit))
        return await self.try_fetch("SELECT * FROM alerts ORDER BY updated_at DESC LIMIT %s", (limit,))

    async def incidents(self, limit: int = 50) -> list[dict[str, Any]] | None:
        return await self.try_fetch("SELECT * FROM incidents ORDER BY opened_tick DESC LIMIT %s", (limit,))

    async def mode_transitions(self, limit: int = 100) -> list[dict[str, Any]] | None:
        return await self.try_fetch("SELECT * FROM mode_transitions ORDER BY id DESC LIMIT %s", (limit,))

    async def demand_series(self, station: str, fuel: str, limit: int = 192) -> list[dict[str, Any]] | None:
        return await self.try_fetch(
            "SELECT d.tick, d.demand, d.served, d.unmet, f.p10, f.p50, f.p90 FROM demand_obs d "
            "LEFT JOIN forecasts f ON f.tick = d.tick AND f.station = d.station AND f.fuel = d.fuel AND f.horizon = 1 "
            "WHERE d.station = %s AND d.fuel = %s ORDER BY d.tick DESC LIMIT %s",
            (station, fuel, limit),
        )

    async def policies(self) -> list[dict[str, Any]] | None:
        return await self.try_fetch("SELECT version, config, active, created_at FROM policies ORDER BY created_at")

    async def save_policy(self, version: str, config: dict[str, Any], active: bool) -> None:
        await self.execute(
            "INSERT INTO policies (version, config, active) VALUES (%s, %s, %s) "
            "ON CONFLICT (version) DO UPDATE SET config = EXCLUDED.config",
            (version, json.dumps(config), active),
        )

    async def activate_policy(self, version: str) -> None:
        await self.execute("UPDATE policies SET active = (version = %s)", (version,))


db = Database(settings.database_url)
