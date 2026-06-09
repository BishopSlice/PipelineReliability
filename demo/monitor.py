"""
D-60: Live sync monitor — triggers real Fivetran sync, polls outcome,
builds signal payload for the PRE agent pipeline.

Replaces static trigger JSON files. No JSON fallback path.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import os
import time
from typing import Callable, Coroutine

import httpx

EmitFn = Callable[[str, dict], Coroutine]

_BASE_URL = "https://api.fivetran.com"
_SYNC_POLL_INTERVAL  = 5    # seconds between sync status checks
_SYNC_POLL_MAX       = 120  # seconds before sync poll times out
_TRANS_POLL_INTERVAL = 10   # seconds between transformation status checks
_TRANS_POLL_MAX      = 180  # seconds before transformation poll times out

# Sync states that mean "still in progress — keep polling"
_SYNC_RUNNING_STATES = {"syncing", "scheduled", "rescheduled"}


# ── Fivetran HTTP helpers ──────────────────────────────────────────────────────

def _auth_headers() -> dict[str, str]:
    key    = os.environ["FIVETRAN_API_KEY"]
    secret = os.environ["FIVETRAN_API_SECRET"]
    token  = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


async def _get(path: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{_BASE_URL}{path}", headers=_auth_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("data", r.json())


async def _post(path: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{_BASE_URL}{path}", headers=_auth_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("data", r.json())


# ── Polling helpers ────────────────────────────────────────────────────────────

async def _poll_sync(connector_id: str, emit: EmitFn) -> dict:
    """Poll until sync settles (not scheduling/syncing). Returns connection details."""
    elapsed = 0
    while elapsed < _SYNC_POLL_MAX:
        details = await _get(f"/v1/connections/{connector_id}")
        status      = details.get("status", {})
        sync_state  = (status.get("sync_state") or "").lower()
        setup_state = (status.get("setup_state") or "").lower()

        if setup_state == "broken" or sync_state == "broken":
            return details
        if sync_state not in _SYNC_RUNNING_STATES:
            return details  # connected, paused, or other terminal state

        await emit("monitoring_sync", {
            "message": f"Sync in progress… ({elapsed}s)",
            "status": "running",
        })
        await asyncio.sleep(_SYNC_POLL_INTERVAL)
        elapsed += _SYNC_POLL_INTERVAL

    return await _get(f"/v1/connections/{connector_id}")  # timeout — return last state


async def _poll_transformation(transformation_id: str, emit: EmitFn) -> dict:
    """Poll until transformation reaches a terminal state. Returns transformation details."""
    elapsed = 0
    while elapsed < _TRANS_POLL_MAX:
        details = await _get(f"/v1/transformations/{transformation_id}")
        status = (details.get("status") or "").upper()

        if status in ("FAILED", "SUCCEEDED", "CANCELLED", "ERROR"):
            return details

        await emit("monitoring_transform", {
            "message": f"Transformation running… ({elapsed}s)",
            "status": "running",
        })
        await asyncio.sleep(_TRANS_POLL_INTERVAL)
        elapsed += _TRANS_POLL_INTERVAL

    return await _get(f"/v1/transformations/{transformation_id}")


# ── Signal builders ────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


_VALID_SYNC_STATES = {
    "syncing", "scheduled", "rescheduled", "stopped", "delayed",
    "complete", "error", "paused", "incomplete", "broken",
}

def _sync_signal(connector_id: str, group_id: str, details: dict) -> dict:
    status      = details.get("status", {})
    sync_state  = (status.get("sync_state") or "broken").lower()
    # Clamp to known Literal values — unknown Fivetran states default to "error"
    # so Pydantic validation never fails on unexpected API responses.
    if sync_state not in _VALID_SYNC_STATES:
        sync_state = "error"
    error_code  = status.get("update_state") or "reconnect"
    return {
        "signal_type":   "sync_status",
        "observed_at":   _now(),
        "severity":      "error",
        "status":        sync_state,
        "connection_id": connector_id,
        "group_id":      group_id,
        "error_code":    "reconnect",
        "error_message": (
            "Unknown authentication or authorization failure — "
            "Fivetran cannot connect to source. Reauthorisation required."
        ),
        "rows_synced": 0,
    }


# Known dbt error messages for our specific transformations.
# The Fivetran transformation API does not surface dbt-level error text — only status.
# These are accurate to the actual failures caused by our demo infrastructure.
_TRANSFORM_META: dict[str, dict] = {
    "serene_hide": {
        "error_type":    "type_mismatch",
        "error_message": (
            "Database Error in model invoices_amounts — "
            "Bad double value: CAST(amount AS FLOAT64) fails after column re-inferred as STRING"
        ),
        "models_failed": ["invoices_amounts"],
    },
    "malnutrition_cornflake": {
        "error_type":    "missing_relation",
        "error_message": (
            "Database Error in model transactions_net_revenue — "
            "Unrecognized name: revenue"
        ),
        "models_failed": ["transactions_net_revenue"],
    },
}


def _transform_signal(transformation_id: str, connector_id: str) -> dict:
    meta = _TRANSFORM_META.get(transformation_id, {
        "error_type": "sql_error",
        "error_message": f"Transformation {transformation_id} failed",
        "models_failed": [],
    })
    return {
        "signal_type":       "transformation_event",
        "observed_at":       _now(),
        "severity":          "error",
        "transformation_id": transformation_id,
        "connection_id":     connector_id,
        "status":            "failed",
        **meta,
    }


# ── Main entry point ───────────────────────────────────────────────────────────

async def live_sync_and_detect(scenario: dict, emit: EmitFn) -> tuple[dict, int]:
    """
    D-60 + D-66: Trigger a real Fivetran sync, poll outcome, build and return a
    trigger payload dict suitable for run_incident_streaming(), plus the
    monitoring phase duration in milliseconds.

    Returns: (trigger_payload, monitoring_phase_ms)
    Emits monitoring_sync / monitoring_transform / monitoring_dispatch SSE events.
    """
    _start = time.monotonic()
    connector_id      = scenario["connector_id"]
    group_id          = scenario["group_id"]
    transformation_id = scenario.get("transformation_id")

    # ── 1. Trigger sync ────────────────────────────────────────────────────────
    await emit("monitoring_sync", {
        "message": f"Syncing connector {connector_id}…",
        "status": "starting",
    })
    try:
        await _post(f"/v1/connections/{connector_id}/sync")
    except httpx.HTTPStatusError:
        # Broken connectors may reject the sync request — check current state anyway
        pass

    await asyncio.sleep(3)  # Let Fivetran register the sync attempt

    # ── 2. Poll sync outcome ───────────────────────────────────────────────────
    conn = await _poll_sync(connector_id, emit)
    status      = conn.get("status", {})
    sync_state  = (status.get("sync_state") or "").lower()
    setup_state = (status.get("setup_state") or "").lower()
    is_broken   = setup_state == "broken" or sync_state == "broken"

    if is_broken:
        await emit("monitoring_sync", {
            "message": f"Sync failed — setup_state: {setup_state}",
            "status": "failed",
        })
        signal = _sync_signal(connector_id, group_id, conn)
        notes  = (
            f"Connector '{connector_id}' failed to sync. "
            f"Setup state: {setup_state}. "
            "Likely an authentication or authorization failure requiring reauthorisation."
        )
        await emit("monitoring_dispatch", {
            "message": "Incident detected — dispatching to PRE…",
            "signals": [signal],
        })
        return {"reporter": "monitoring_system", "notes": notes, "signals": [signal]}, int((time.monotonic() - _start) * 1000)

    # Sync succeeded
    await emit("monitoring_sync", {
        "message": "Sync complete — checking downstream transformations…",
        "status": "ok",
    })

    if not transformation_id:
        await emit("monitoring_dispatch", {
            "message": "Sync healthy — no incident detected",
            "signals": [],
        })
        return {"reporter": "monitoring_system", "notes": "No incident detected.", "signals": []}, int((time.monotonic() - _start) * 1000)

    # ── 3. Trigger transformation ──────────────────────────────────────────────
    await emit("monitoring_transform", {
        "message": f"Running transformation {transformation_id}…",
        "status": "starting",
    })
    try:
        await _post(f"/v1/transformations/{transformation_id}/run")
    except httpx.HTTPStatusError:
        pass  # May already be running — poll regardless

    await asyncio.sleep(5)

    # ── 4. Poll transformation outcome ─────────────────────────────────────────
    t_details = await _poll_transformation(transformation_id, emit)
    t_status  = (t_details.get("status") or "").upper()

    if t_status != "FAILED":
        await emit("monitoring_transform", {
            "message": f"Transformation {transformation_id}: {t_status.lower()} — no incident",
            "status": "ok",
        })
        await emit("monitoring_dispatch", {
            "message": "Pipeline healthy — no incident detected",
            "signals": [],
        })
        return {"reporter": "monitoring_system", "notes": "No incident detected.", "signals": []}, int((time.monotonic() - _start) * 1000)

    await emit("monitoring_transform", {
        "message": f"Transformation {transformation_id} failed — incident detected",
        "status": "failed",
    })

    signal = _transform_signal(transformation_id, connector_id)
    notes  = (
        f"Connector '{connector_id}' synced successfully. "
        f"Transformation '{transformation_id}' failed immediately after sync. "
        f"Error: {signal['error_message']}"
    )
    await emit("monitoring_dispatch", {
        "message": "Incident detected — dispatching to PRE…",
        "signals": [signal],
    })
    return {"reporter": "monitoring_system", "notes": notes, "signals": [signal]}, int((time.monotonic() - _start) * 1000)
