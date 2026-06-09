"""
Eval DB — Firestore (primary) with SQLite fallback for local development.

Firestore is active when GOOGLE_CLOUD_PROJECT is set (Cloud Run and any GCP
environment). SQLite activates automatically when GOOGLE_CLOUD_PROJECT is
absent, keeping local development and offline testing functional with no setup.

D-54: Firestore is the production store.
D-57: SQLite implementation preserved below — do not delete.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Gemini 3.1 Flash-Lite pricing (USD per 1M tokens) — updated per D-44
_COST_PER_1M_IN  = 0.25
_COST_PER_1M_OUT = 1.50

_BASELINE_MIN_RUNS = 5
_USE_FIRESTORE = bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _compute_cost(tokens_in: int, tokens_out: int) -> float:
    return round(
        (tokens_in / 1_000_000) * _COST_PER_1M_IN
        + (tokens_out / 1_000_000) * _COST_PER_1M_OUT,
        6,
    )


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    idx = max(0, int(len(values) * pct) - 1)
    return sorted(values)[idx]


# ── Firestore implementation ───────────────────────────────────────────────────

if _USE_FIRESTORE:
    import logging as _log
    try:
        from google.cloud import firestore as _fs
        _db = _fs.Client()
        # Validate connectivity on startup with a lightweight call
        list(_db.collection("pre_runs").limit(1).stream())
        _FIRESTORE_OK = True
    except Exception as _fs_init_err:
        _log.getLogger(__name__).warning(
            "Firestore unavailable (%s: %s) — falling back to SQLite for eval DB",
            type(_fs_init_err).__name__, str(_fs_init_err)[:120],
        )
        _USE_FIRESTORE = False
        _FIRESTORE_OK = False

if _USE_FIRESTORE:
    _COLLECTION = "pre_runs"

    def init_db() -> None:
        pass  # Firestore requires no schema initialisation

    def insert_run(data: dict) -> None:
        tokens_in  = data.get("tokens_in") or 0
        tokens_out = data.get("tokens_out") or 0
        doc = {
            **data,
            "estimated_cost_usd":  _compute_cost(tokens_in, tokens_out),
            "agent_dispatch_path": data.get("agent_dispatch_path") or [],
            "recovery_confirmed":  bool(data.get("recovery_confirmed", False)),
            "handoff_occurred":    bool(data.get("handoff_occurred", False)),
            "error_occurred":      bool(data.get("error_occurred", False)),
            "monitoring_phase_ms":  data.get("monitoring_phase_ms") or 0,
            "agent_phase_ms":       data.get("agent_phase_ms") or 0,
            "otps":                 data.get("otps"),
            "per_agent_duration_ms": data.get("per_agent_duration_ms") or {},
            "per_agent_tokens":     data.get("per_agent_tokens") or {},
        }
        _db.collection(_COLLECTION).document(data["run_id"]).set(doc)

    def get_runs(limit: int = 200) -> list[dict]:
        docs = (
            _db.collection(_COLLECTION)
            .order_by("timestamp", direction=_fs.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [d.to_dict() for d in docs]

    def get_baselines() -> dict | None:
        all_docs = [
            d.to_dict()
            for d in (
                _db.collection(_COLLECTION)
                .order_by("timestamp", direction=_fs.Query.DESCENDING)
                .limit(200)
                .stream()
            )
        ]
        rows = [r for r in all_docs if not r.get("error_occurred")]

        if len(rows) < _BASELINE_MIN_RUNS:
            return None

        durations     = [r["duration_ms"] for r in rows if r.get("duration_ms") is not None]
        costs         = [r["estimated_cost_usd"] for r in rows if r.get("estimated_cost_usd") is not None]
        monitoring_ms = [r["monitoring_phase_ms"] for r in rows if r.get("monitoring_phase_ms")]
        agent_ms      = [r["agent_phase_ms"] for r in rows if r.get("agent_phase_ms")]
        ttft_ms       = [r["first_event_latency_ms"] for r in rows if r.get("first_event_latency_ms")]
        otps_vals     = [r["otps"] for r in rows if r.get("otps")]

        return {
            "run_count":         len(rows),
            "min_runs":          _BASELINE_MIN_RUNS,
            "p95_duration_ms":   _percentile(durations, 0.95),
            "p95_cost_usd":      _percentile(costs, 0.95),
            "p50_duration_ms":   _percentile(durations, 0.50),
            "p50_cost_usd":      _percentile(costs, 0.50),
            "p95_monitoring_ms": _percentile(monitoring_ms, 0.95),
            "p50_monitoring_ms": _percentile(monitoring_ms, 0.50),
            "p95_agent_ms":      _percentile(agent_ms, 0.95),
            "p50_agent_ms":      _percentile(agent_ms, 0.50),
            "p95_ttft_ms":       _percentile(ttft_ms, 0.95),
            "p50_ttft_ms":       _percentile(ttft_ms, 0.50),
            "p95_otps":          _percentile(otps_vals, 0.95),
            "p50_otps":          _percentile(otps_vals, 0.50),
        }

# ── SQLite fallback — active when GOOGLE_CLOUD_PROJECT is not set ─────────────
# D-57: preserved for local development and offline testing.
# To restore as primary store: swap the _USE_FIRESTORE conditional above.
else:
    import sqlite3

    _DB_PATH = Path(__file__).parent / "runs.db"

    _DDL = """
    CREATE TABLE IF NOT EXISTS runs (
        run_id                TEXT PRIMARY KEY,
        scenario_id           TEXT NOT NULL,
        scenario_title        TEXT,
        connector_id          TEXT,
        timestamp             TEXT NOT NULL,
        duration_ms           INTEGER,
        tokens_in             INTEGER,
        tokens_out            INTEGER,
        estimated_cost_usd    REAL,
        root_cause_layer      TEXT,
        status                TEXT,
        confidence            REAL,
        recovery_confirmed    INTEGER,
        agent_dispatch_path   TEXT,
        tool_call_count       INTEGER,
        hitl_gate_count       INTEGER,
        handoff_occurred      INTEGER,
        retry_count           INTEGER,
        first_event_latency_ms INTEGER,
        error_occurred        INTEGER,
        monitoring_phase_ms   INTEGER,
        agent_phase_ms        INTEGER,
        otps                  REAL,
        per_agent_duration_ms TEXT,
        per_agent_tokens      TEXT
    );
    """

    _INSERT = """
    INSERT OR REPLACE INTO runs (
        run_id, scenario_id, scenario_title, connector_id, timestamp, duration_ms,
        tokens_in, tokens_out, estimated_cost_usd, root_cause_layer,
        status, confidence, recovery_confirmed, agent_dispatch_path,
        tool_call_count, hitl_gate_count, handoff_occurred, retry_count,
        first_event_latency_ms, error_occurred, monitoring_phase_ms, agent_phase_ms,
        otps, per_agent_duration_ms, per_agent_tokens
    ) VALUES (
        :run_id, :scenario_id, :scenario_title, :connector_id, :timestamp, :duration_ms,
        :tokens_in, :tokens_out, :estimated_cost_usd, :root_cause_layer,
        :status, :confidence, :recovery_confirmed, :agent_dispatch_path,
        :tool_call_count, :hitl_gate_count, :handoff_occurred, :retry_count,
        :first_event_latency_ms, :error_occurred, :monitoring_phase_ms, :agent_phase_ms,
        :otps, :per_agent_duration_ms, :per_agent_tokens
    );
    """

    def _conn() -> sqlite3.Connection:
        c = sqlite3.connect(_DB_PATH)
        c.row_factory = sqlite3.Row
        return c

    def init_db() -> None:
        with _conn() as c:
            c.executescript(_DDL)
            # Safe migrations — add any column that doesn't exist yet.
            # Keeps existing DBs in sync when new columns are added to _DDL.
            for col, typ in [
                ("monitoring_phase_ms",   "INTEGER"),
                ("agent_phase_ms",        "INTEGER"),
                ("otps",                  "REAL"),
                ("per_agent_duration_ms", "TEXT"),
                ("per_agent_tokens",      "TEXT"),
                ("connector_id",          "TEXT"),
            ]:
                try:
                    c.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass  # column already exists

    def insert_run(data: dict) -> None:
        tokens_in  = data.get("tokens_in") or 0
        tokens_out = data.get("tokens_out") or 0
        row = {
            **data,
            "estimated_cost_usd":  _compute_cost(tokens_in, tokens_out),
            "agent_dispatch_path": json.dumps(data.get("agent_dispatch_path") or []),
            "recovery_confirmed":  int(bool(data.get("recovery_confirmed", False))),
            "handoff_occurred":    int(bool(data.get("handoff_occurred", False))),
            "error_occurred":      int(bool(data.get("error_occurred", False))),
            "monitoring_phase_ms":  data.get("monitoring_phase_ms") or 0,
            "agent_phase_ms":       data.get("agent_phase_ms") or 0,
            "otps":                 data.get("otps"),
            "per_agent_duration_ms": json.dumps(data.get("per_agent_duration_ms") or {}),
            "per_agent_tokens":     json.dumps(data.get("per_agent_tokens") or {}),
        }
        with _conn() as c:
            c.execute(_INSERT, row)

    def get_runs(limit: int = 200) -> list[dict]:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            d["agent_dispatch_path"]   = json.loads(d.get("agent_dispatch_path") or "[]")
            d["per_agent_duration_ms"] = json.loads(d.get("per_agent_duration_ms") or "{}")
            d["per_agent_tokens"]      = json.loads(d.get("per_agent_tokens") or "{}")
            d["recovery_confirmed"]    = bool(d["recovery_confirmed"])
            d["handoff_occurred"]      = bool(d["handoff_occurred"])
            d["error_occurred"]        = bool(d["error_occurred"])
            result.append(d)
        return result

    def get_baselines() -> dict | None:
        with _conn() as c:
            rows = c.execute(
                "SELECT duration_ms, estimated_cost_usd, monitoring_phase_ms, agent_phase_ms, "
                "first_event_latency_ms, otps "
                "FROM runs WHERE error_occurred = 0"
            ).fetchall()

        if len(rows) < _BASELINE_MIN_RUNS:
            return None

        durations     = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
        costs         = [r["estimated_cost_usd"] for r in rows if r["estimated_cost_usd"] is not None]
        monitoring_ms = [r["monitoring_phase_ms"] for r in rows if r["monitoring_phase_ms"]]
        agent_ms      = [r["agent_phase_ms"] for r in rows if r["agent_phase_ms"]]
        ttft_ms       = [r["first_event_latency_ms"] for r in rows if r["first_event_latency_ms"]]
        otps_vals     = [r["otps"] for r in rows if r["otps"]]

        return {
            "run_count":         len(rows),
            "min_runs":          _BASELINE_MIN_RUNS,
            "p95_duration_ms":   _percentile(durations, 0.95),
            "p95_cost_usd":      _percentile(costs, 0.95),
            "p50_duration_ms":   _percentile(durations, 0.50),
            "p50_cost_usd":      _percentile(costs, 0.50),
            "p95_monitoring_ms": _percentile(monitoring_ms, 0.95),
            "p50_monitoring_ms": _percentile(monitoring_ms, 0.50),
            "p95_agent_ms":      _percentile(agent_ms, 0.95),
            "p50_agent_ms":      _percentile(agent_ms, 0.50),
            "p95_ttft_ms":       _percentile(ttft_ms, 0.95),
            "p50_ttft_ms":       _percentile(ttft_ms, 0.50),
            "p95_otps":          _percentile(otps_vals, 0.95),
            "p50_otps":          _percentile(otps_vals, 0.50),
        }
