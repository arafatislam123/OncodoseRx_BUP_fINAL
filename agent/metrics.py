"""Prometheus metrics for the agent. Names follow section 17.1 of the design doc."""

from prometheus_client import Counter, Gauge, Histogram

# --- application ---------------------------------------------------------
SIM_REQUESTS = Counter("fsa_sim_http_requests_total", "Calls to the simulator", ["endpoint", "code"])
SIM_LATENCY = Histogram(
    "fsa_sim_http_latency_seconds", "Simulator call latency", ["endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3),
)
SSE_CONNECTED = Gauge("fsa_sse_connected", "1 when the SSE stream is connected")
SSE_RECONNECTS = Counter("fsa_sse_reconnects_total", "SSE reconnect attempts")
MODE = Gauge("fsa_mode", "Current operating mode (1 for the active one)", ["mode"])
DATA_AGE = Gauge("fsa_data_age_ticks", "Age of cached data in ticks", ["resource"])
CYCLE_DURATION = Histogram(
    "fsa_cycle_duration_seconds", "Agent cycle duration",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.125, 0.25, 0.5, 1, 2, 5),
)
CYCLE_SKIPPED = Counter("fsa_cycle_skipped_ticks_total", "Ticks skipped because a cycle ran long")
LAND_LAG = Gauge("fsa_land_lag_ticks", "Ticks between snapshot and order landing")
OUTBOX_DEPTH = Gauge("fsa_outbox_depth", "Records waiting to be written to Postgres")
INVALID_RESPONSES = Counter("fsa_invalid_response_total", "Simulator responses rejected by validation", ["endpoint"])

# --- domain --------------------------------------------------------------
SERVICE_LEVEL = Gauge("sim_service_level", "Service level reported by the simulator")
UNMET_LITERS = Gauge("sim_unmet_liters", "Unmet demand reported by the simulator")
STATION_INVENTORY = Gauge("station_inventory_liters", "Station inventory", ["station", "fuel"])
DEPOT_INVENTORY = Gauge("depot_inventory_liters", "Depot inventory", ["depot", "fuel"])
STOCKOUT_PROB = Gauge("stockout_probability", "Projected stockout probability without new action", ["station", "fuel"])
ALLOCATIONS_SUBMITTED = Counter("fsa_allocations_submitted_total", "Allocation POST outcomes", ["result"])
ALLOCATION_REJECTIONS = Counter("fsa_allocation_rejections_total", "Allocation rejections by code", ["code"])
INTENTS = Gauge("fsa_intents", "Intents by state", ["state"])

# --- intelligence --------------------------------------------------------
FORECAST_APE = Gauge("fsa_forecast_abs_pct_error", "Rolling forecast MAPE", ["station", "fuel"])
FORECAST_COVERAGE = Gauge("fsa_forecast_interval_coverage", "Share of observations inside P10-P90")
DECISION_CONFIDENCE = Histogram(
    "fsa_decision_confidence", "Confidence of recommendations",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
ALERTS_RAISED = Counter("fsa_alerts_raised_total", "Alerts raised", ["type", "severity"])
DECISIONS = Counter("fsa_decisions_total", "Decisions by gate result", ["gate"])
FALLBACKS = Counter("fsa_fallback_activations_total", "Fallback activations", ["reason"])
PLANNER_SECONDS = Histogram(
    "fsa_planner_solve_seconds", "Planner solve time",
    buckets=(0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.15, 0.25, 0.5),
)
