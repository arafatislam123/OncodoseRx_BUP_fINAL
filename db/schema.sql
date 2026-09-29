-- History and read models (design section 15).
-- Loaded by Postgres on first start, and applied again by the agent and api on
-- startup, so every statement has to be safe to run twice.

CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    cycle_id    TEXT NOT NULL,
    tick        INTEGER NOT NULL,
    station     TEXT NOT NULL,
    fuel        TEXT NOT NULL,
    gate        TEXT NOT NULL,
    confidence  REAL,
    record      JSONB NOT NULL,
    explanation TEXT,
    outcome     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decisions_tick ON decisions (tick DESC);
CREATE INDEX IF NOT EXISTS decisions_station ON decisions (station, fuel);

CREATE TABLE IF NOT EXISTS alerts (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    entity          TEXT NOT NULL,
    fuel            TEXT,
    state           TEXT NOT NULL,
    message         TEXT NOT NULL,
    first_seen_tick INTEGER NOT NULL,
    last_seen_tick  INTEGER NOT NULL,
    evidence        JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alerts_state ON alerts (state, updated_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    opened_tick INTEGER NOT NULL,
    closed_tick INTEGER,
    alert_ids   JSONB NOT NULL,
    summary     TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recommendations (
    id           TEXT PRIMARY KEY,
    decision_id  TEXT,
    state        TEXT NOT NULL,
    station      TEXT NOT NULL,
    fuel         TEXT NOT NULL,
    quantity     REAL NOT NULL,
    expires_tick INTEGER,
    payload      JSONB NOT NULL,
    reviewer     TEXT,
    reviewed_at  TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recommendations_state ON recommendations (state);

CREATE TABLE IF NOT EXISTS mode_transitions (
    id        BIGSERIAL PRIMARY KEY,
    from_mode TEXT NOT NULL,
    to_mode   TEXT NOT NULL,
    reason    TEXT,
    wall      TIMESTAMPTZ NOT NULL,
    tick      INTEGER
);

CREATE TABLE IF NOT EXISTS demand_obs (
    epoch   INTEGER NOT NULL,
    id      BIGINT NOT NULL,
    station TEXT NOT NULL,
    fuel    TEXT NOT NULL,
    tick    INTEGER NOT NULL,
    demand  REAL NOT NULL,
    served  REAL NOT NULL,
    unmet   REAL NOT NULL,
    PRIMARY KEY (epoch, id)
);
CREATE INDEX IF NOT EXISTS demand_obs_series ON demand_obs (station, fuel, tick);

CREATE TABLE IF NOT EXISTS forecasts (
    tick    INTEGER NOT NULL,
    station TEXT NOT NULL,
    fuel    TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    p10     REAL NOT NULL,
    p50     REAL NOT NULL,
    p90     REAL NOT NULL,
    PRIMARY KEY (tick, station, fuel, horizon)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id        BIGSERIAL PRIMARY KEY,
    tick      INTEGER NOT NULL,
    cycle_id  TEXT,
    resource  TEXT NOT NULL,
    data      JSONB NOT NULL,
    stale     BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS snapshots_cycle ON snapshots (cycle_id);

CREATE TABLE IF NOT EXISTS policies (
    version    TEXT PRIMARY KEY,
    config     JSONB NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
