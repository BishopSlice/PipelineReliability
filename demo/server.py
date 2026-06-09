from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root must be in path before any project imports
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demo.runner import run_incident_streaming
from demo.monitor import live_sync_and_detect
from demo.db import init_db, get_runs, get_baselines, _BASELINE_MIN_RUNS
from integration.cloud_trace import setup_tracing

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="PRE — Pipeline Reliability Engineer")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

_trace_provider = setup_tracing()
init_db()

# ── Scenario registry ──────────────────────────────────────────────────────────

# Three curated scenarios, each backed by a real Fivetran connector in a
# genuinely broken state (see D-36, D-56, D-60).
# trigger_file keys removed — D-60: live sync replaces static JSON.
SCENARIOS: dict[str, dict] = {
    # ── Connector layer ────────────────────────────────────────────────────────
    "connector_accept": {
        "id":               "connector_accept",
        "title":            "Connector Failure",
        "description":      "Expired OAuth credentials on Google Sheets. HITL gate fires — approve to reconnect, deny to escalate.",
        "severity":         "critical",
        "layer":            "connector",
        "connector_id":     "amplify_sandpit",
        "group_id":         "atrophy_coal",
        "transformation_id": None,
        "hitl":             True,
        "info": {
            "trigger":  "Google Sheets connection returning 401 — OAuth token expired, all syncs failing",
            "expected": "Connector layer diagnosis → HITL gate fires if a fix is available → post-fix validation → resolved if recovery confirmed, partial if validation is limited by tool coverage",
            "tools":    ["get_connection_details", "get_connection_state", "run_connection_setup_tests", "request_approval", "create_connect_card"],
        },
    },
    # ── Schema layer ───────────────────────────────────────────────────────────
    "schema_cascade": {
        "id":               "schema_cascade",
        "title":            "Schema Cascade",
        "description":      "Column type change in source sheet triggers downstream transformation failure. Schema and Transformation agents hand off.",
        "severity":         "error",
        "layer":            "schema",
        "connector_id":     "might_generate",
        "group_id":         "atrophy_coal",
        "transformation_id": "serene_hide",
        "hitl":             False,
        "info": {
            "trigger":  "Column type changed from NUMERIC to STRING in source sheet — downstream transformation immediately failed",
            "expected": "Schema agent confirms type_changed via schema config → hands off to Transformation agent → both escalate",
            "tools":    ["get_connection_schema_config", "reload_connection_schema_config", "list_transformations", "get_transformation_details"],
        },
    },
    "column_excluded": {
        "id":               "column_excluded",
        "title":            "Column Excluded",
        "description":      "A revenue column was excluded from sync via Fivetran schema config. Transformation fails with missing_relation.",
        "severity":         "error",
        "layer":            "schema",
        "connector_id":     "unusable_touched",
        "group_id":         "atrophy_coal",
        "transformation_id": "malnutrition_cornflake",
        "hitl":             False,
        "info": {
            "trigger":  "Column 'revenue' excluded from sync via Fivetran schema config — transformation reporting missing_relation",
            "expected": "Schema agent reads column_excluded from schema config → Transformation agent confirms missing relation → escalated",
            "tools":    ["get_connection_schema_config", "list_transformations", "get_transformation_details"],
        },
    },
}

# ── Disabled scenarios (D-56) — preserved per D-57, do not delete ─────────────
# Removed because they cannot be backed by real Fivetran account state with
# clean API confirmation. See D-56 for per-scenario rationale.
# To re-enable a scenario: move its entry back into SCENARIOS above.
_SCENARIOS_DISABLED: dict[str, dict] = {
    "sync_failure": {
        "id": "sync_failure",
        "title": "Sync Incomplete",
        "description": "Scheduled sync completed in 45s with 0 rows (expected 5000). Connector agent investigates the zero-row incomplete sync.",
        "severity": "error",
        "layer": "connector",
        "trigger_file": "trigger_sync_failure.json",
        "hitl": False,
        "info": {
            "trigger": "Sync completed in 45s (typical: 8 min) with 0 of 5,000 expected rows — INCOMPLETE status",
            "expected": "Connector layer diagnosis → agent may propose a manual sync via HITL if it identifies a recoverable cause → escalated if recovery unconfirmable",
            "tools": ["get_connection_details", "get_connection_state", "list_connections_in_group"],
        },
    },
    "auto_paused": {
        "id": "auto_paused",
        "title": "Auto-Paused Connector",
        "description": "Fivetran auto-paused the connector after 5 consecutive auth failures. Connector agent determines root cause and recommends re-enable path.",
        "severity": "error",
        "layer": "connector",
        "trigger_file": "trigger_auto_paused.json",
        "hitl": False,
        "info": {
            "trigger": "Fivetran auto-paused connector after 5 consecutive CONNECTOR_AUTH_ERROR failures",
            "expected": "Connector agent reads pause reason and prior error codes → confirms auth as root cause → escalated with re-enable recommendation",
            "tools": ["get_connection_details", "get_connection_state", "run_connection_setup_tests"],
        },
    },
    "schema_column_added": {
        "id": "schema_column_added",
        "title": "Schema Expansion",
        "description": "New column added to source sheet. Transformation fails immediately — schema and transformation agents trace the break.",
        "severity": "warning",
        "layer": "schema",
        "trigger_file": "trigger_schema_column_added.json",
        "hitl": False,
        "info": {
            "trigger": "New column 'discount_code' appeared in source Google Sheet — transformation orders_summary failing",
            "expected": "Schema agent identifies column_added → Transformation agent confirms model break → escalated",
            "tools": ["get_connection_schema_config", "list_transformations", "get_transformation_details"],
        },
    },
    "data_quality": {
        "id": "data_quality",
        "title": "Data Quality Alert",
        "description": "Duplicate rate spike on the orders table. DQ agent traces the issue to a source-level problem in Google Sheets.",
        "severity": "warning",
        "layer": "data_quality",
        "trigger_file": "trigger_dq.json",
        "hitl": False,
        "info": {
            "trigger": "Duplicate rate at 15% on orders table — 15× above 1% baseline, spike began after last sync",
            "expected": "DQ agent investigates source data via schema and connection tools → traces to upstream connector or escalates directly",
            "tools": ["get_connection_details", "get_connection_schema_config", "list_transformations"],
        },
    },
}


# ── Per-run state ──────────────────────────────────────────────────────────────

@dataclass
class RunState:
    run_id: str
    scenario_id: str
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    gate_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None


_runs: dict[str, RunState] = {}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def landing():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/demo")
async def demo_page():
    return FileResponse(_STATIC_DIR / "demo.html")


@app.get("/evals")
async def evals_page():
    return FileResponse(_STATIC_DIR / "evals.html")


@app.get("/api/runs")
async def api_runs():
    try:
        return get_runs()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("api_runs failed: %s", exc)
        return []


@app.get("/api/baselines")
async def api_baselines():
    """P95/P50 latency and cost baselines. Returns null fields until min_runs completed runs exist."""
    try:
        result = get_baselines()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("api_baselines failed: %s", exc)
        result = None
    if result is None:
        return {
            "run_count": 0,
            "min_runs": _BASELINE_MIN_RUNS,
            "p95_duration_ms": None,
            "p95_cost_usd": None,
            "p50_duration_ms": None,
            "p50_cost_usd": None,
            "ready": False,
        }
    return {**result, "ready": True}


@app.get("/api/scenarios")
async def get_scenarios():
    return list(SCENARIOS.values())


class RunRequest(BaseModel):
    scenario_id: str


@app.post("/api/run")
async def start_run(req: RunRequest):
    if req.scenario_id not in SCENARIOS:
        raise HTTPException(404, "Scenario not found")

    scenario = SCENARIOS[req.scenario_id]
    run_id   = str(uuid.uuid4())[:8]
    state    = RunState(run_id=run_id, scenario_id=req.scenario_id)
    _runs[run_id] = state

    async def emit(event_type: str, data: dict) -> None:
        await state.event_queue.put({"type": event_type, "data": data})

    async def run_task() -> None:
        try:
            # D-60 + D-66: live sync monitoring phase; returns (payload, monitoring_phase_ms)
            trigger_payload, monitoring_phase_ms = await live_sync_and_detect(scenario, emit)
            if not trigger_payload.get("signals"):
                # No incident detected — nothing to run
                return
            await run_incident_streaming(
                trigger_payload, emit, state.gate_queue,
                run_id=run_id,
                scenario_id=req.scenario_id,
                scenario_title=scenario.get("title", req.scenario_id),
                connector_id=scenario.get("connector_id", ""),
                monitoring_phase_ms=monitoring_phase_ms,
            )
        except Exception as exc:
            await emit("error", {"message": str(exc)})
        finally:
            await state.event_queue.put(None)  # sentinel → SSE done

    state.task = asyncio.create_task(run_task())
    return {"run_id": run_id}


@app.get("/api/stream/{run_id}")
async def stream_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")

    state = _runs[run_id]

    async def generator():
        while True:
            item = await state.event_queue.get()
            if item is None:
                yield 'data: {"type":"done"}\n\n'
                break
            yield f"data: {json.dumps(item, default=str)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class GateDecision(BaseModel):
    run_id: str
    approved: bool


@app.post("/api/gate/respond")
async def gate_respond(decision: GateDecision):
    if decision.run_id not in _runs:
        raise HTTPException(404, "Run not found")
    await _runs[decision.run_id].gate_queue.put(decision.approved)
    return {"ok": True}


# ── Test runner ───────────────────────────────────────────────────────────────

_SUMMARY_RE = re.compile(
    r"(\d+ passed)?.*?(\d+ failed)?.*?(\d+ error)?.*?(\d+ warning)?",
    re.IGNORECASE,
)


@app.get("/api/test/stream")
async def stream_tests():
    """Run pytest and stream output line-by-line as SSE."""

    async def generator():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short",
            cwd=str(_PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        summary_line = ""
        passed = failed = 0

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            yield f"data: {json.dumps({'type': 'line', 'text': line})}\n\n"

            # Detect summary line: "X passed, Y failed in Zs"
            if "passed" in line or "failed" in line or "error" in line:
                m = re.search(r"(\d+) passed", line)
                if m:
                    passed = int(m.group(1))
                m = re.search(r"(\d+) failed", line)
                if m:
                    failed = int(m.group(1))
                if re.search(r"\d+ (passed|failed)", line) and "in " in line:
                    summary_line = line.strip()

        await proc.wait()

        counts = f"{passed} passed" + (f", {failed} failed" if failed else "")
        yield f"data: {json.dumps({'type': 'summary', 'line': summary_line or counts, 'counts': counts, 'passed': passed, 'failed': failed})}\n\n"
        yield 'data: {"type":"done"}\n\n'

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ────────────────────────────────────────────────────────────────

@app.on_event("shutdown")
async def _shutdown():
    if _trace_provider:
        _trace_provider.force_flush()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("demo.server:app", host="0.0.0.0", port=8000, reload=False)
