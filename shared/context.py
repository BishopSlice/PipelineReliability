from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.genai import types

from models import (
    AgentFinding,
    IncidentContext,
)

logger = logging.getLogger(__name__)

INCIDENT_CONTEXT_KEY = "incident_context"


_VALID_UPSTREAM = {"connector", "schema", "transformation", None}
_VALID_AGENT_IDS = {"connector", "schema", "transformation", "data_quality"}


_VALID_FINDING_STATUSES = {"running", "complete", "escalated", "handed_off", "partial"}
_VALID_SCHEMA_FIX_STATUSES = {"applied", "pending_approval", "not_required"}

def _coerce_agent_finding(raw: dict) -> None:
    """Coerce LLM-invented values to valid Pydantic literals, in-place."""
    urc = raw.get("upstream_root_cause")
    if urc not in _VALID_UPSTREAM:
        raw["upstream_root_cause"] = None

    if raw.get("agent_id") not in _VALID_AGENT_IDS:
        raw["agent_id"] = "connector"

    # AgentFinding.status — LLM sometimes returns "error", "timeout", "failed"
    if raw.get("status") not in _VALID_FINDING_STATUSES:
        raw["status"] = "escalated"

    # SchemaToTransformationHandoff.schema_fix_status — guard before model_validate
    hc = raw.get("handoff_context")
    if isinstance(hc, dict) and "schema_fix_status" in hc:
        if hc["schema_fix_status"] not in _VALID_SCHEMA_FIX_STATUSES:
            hc["schema_fix_status"] = "pending_approval"

    # Null out validation_signal if any required field is None — a diagnostic
    # agent (e.g. data_quality_agent running outside the LoopAgent) has no real
    # recovery metrics to report, so the LLM sends nulls. Better to drop it
    # than fail Pydantic validation on ValidationSignal.description / .observed.
    vs = raw.get("validation_signal")
    if isinstance(vs, dict):
        if vs.get("description") is None or vs.get("observed") is None:
            raw["validation_signal"] = None

    # If handoff_context can't be matched to any known shape, null it out
    # rather than crashing — the orchestrator reads agent_findings to decide
    # next steps, and a missing handoff_context is better than a hard failure.
    if "handoff_context" in raw and raw["handoff_context"] is not None:
        from models import (
            ConnectorToSchemaHandoff, SchemaToTransformationHandoff,
            SchemaToDataQualityHandoff, TransformationToDataQualityHandoff,
        )
        hc = raw["handoff_context"]
        matched = False
        for cls in (ConnectorToSchemaHandoff, SchemaToTransformationHandoff,
                    SchemaToDataQualityHandoff, TransformationToDataQualityHandoff):
            try:
                cls.model_validate(hc)
                matched = True
                break
            except Exception:
                pass
        if not matched:
            raw["handoff_context"] = None


def _strip_markdown_json(text: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` fences that LLMs often wrap output in."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop first line (```json or ```) and last line (```)
        inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()
    return text

# ── Read / Write helpers ───────────────────────────────────────────────────────

def get_incident_context(state: Any) -> IncidentContext:
    """
    Read IncidentContext from ADK session state.
    Accepts either a dict or an ADK state object (which supports dict-style access).
    """
    if hasattr(state, "to_dict"):
        raw = state.to_dict().get(INCIDENT_CONTEXT_KEY)
    else:
        raw = state.get(INCIDENT_CONTEXT_KEY)

    if raw is None:
        raise ValueError(
            f"Key '{INCIDENT_CONTEXT_KEY}' not found in session state. "
            "Orchestrator must initialise IncidentContext before dispatching sub-agents."
        )

    if isinstance(raw, str):
        raw = json.loads(raw)

    return IncidentContext.model_validate(raw)


def set_incident_context(state: Any, context: IncidentContext) -> None:
    """Write IncidentContext back to ADK session state."""
    context.updated_at = datetime.utcnow()
    serialised = context.model_dump(mode="json")

    if hasattr(state, "__setitem__"):
        state[INCIDENT_CONTEXT_KEY] = serialised
    else:
        raise TypeError(f"Cannot write to state object of type {type(state)}")


# ── after_agent_callback ───────────────────────────────────────────────────────

def after_agent_callback(callback_context: CallbackContext) -> types.Content | None:
    """
    Deterministically syncs agent output to IncidentContext after every agent turn.

    Called by ADK after every LlmAgent turn. Reads the agent's structured output
    (stored under output_key), validates it against AgentFinding, and appends it
    to IncidentContext.agent_findings.

    Returns None to preserve the agent's original text output unchanged.
    """
    agent_name = callback_context.agent_name
    output_key = f"{agent_name}_output"

    try:
        raw_output = callback_context.state.get(output_key)
        if not raw_output:  # None or empty string — agent produced no structured output
            logger.warning(
                "after_agent_callback: no output found at key '%s' for agent '%s'",
                output_key, agent_name
            )
            return None

        if isinstance(raw_output, str):
            raw_output = json.loads(_strip_markdown_json(raw_output))

        # Coerce upstream_root_cause to allowed literals (LLM sometimes returns custom strings)
        _coerce_agent_finding(raw_output)

        finding = AgentFinding.model_validate(raw_output)
        finding.completed_at = datetime.utcnow()

        context = get_incident_context(callback_context.state)
        context.agent_findings.append(finding)

        _trim_context_if_needed(context)

        set_incident_context(callback_context.state, context)

        logger.info(
            "after_agent_callback: synced finding for agent '%s' (status=%s, confidence=%.2f)",
            agent_name, finding.status, finding.confidence
        )

    except Exception as e:
        logger.error(
            "after_agent_callback: failed to sync finding for agent '%s': %s",
            agent_name, str(e)
        )

    return None


def after_agent_callback_with_loop_exit(
    callback_context: CallbackContext,
) -> types.Content | None:
    """
    Variant of after_agent_callback for data_quality_agent inside LoopAgent.

    After syncing the finding, checks if the agent signalled loop exit
    (signal_loop_exit = True in its output). If so, sets escalate to terminate
    the LoopAgent cleanly.
    """
    agent_name = callback_context.agent_name
    output_key = f"{agent_name}_output"

    signal_exit = False

    try:
        raw_output = callback_context.state.get(output_key)
        if not raw_output:  # None or empty string
            return None

        if isinstance(raw_output, str):
            raw_output = json.loads(_strip_markdown_json(raw_output))

        _coerce_agent_finding(raw_output)

        # Check for loop exit signal BEFORE validating (it's an extra field)
        signal_exit = bool(raw_output.pop("signal_loop_exit", False))

        finding = AgentFinding.model_validate(raw_output)
        finding.completed_at = datetime.utcnow()

        context = get_incident_context(callback_context.state)
        context.agent_findings.append(finding)
        _trim_context_if_needed(context)
        set_incident_context(callback_context.state, context)

    except Exception as e:
        logger.error(
            "after_agent_callback_with_loop_exit: failed for agent '%s': %s",
            agent_name, str(e)
        )

    if signal_exit:
        logger.info(
            "after_agent_callback_with_loop_exit: recovery confirmed by '%s', signalling loop exit",
            agent_name
        )
        callback_context.actions.escalate = True

    return None


# ── Context size management ────────────────────────────────────────────────────

_MAX_FINDINGS = 12   # hard cap — prevents unbounded growth in cascading incidents


def _trim_context_if_needed(context: IncidentContext) -> None:
    """
    Keep IncidentContext lean in two passes:

    Pass 1 — hard cap: if agent_findings exceeds _MAX_FINDINGS, drop the oldest
    entries beyond the cap and log a warning. The most recent _MAX_FINDINGS are kept.

    Pass 2 — size trim: if the serialised context still exceeds 6 KB, compact older
    duplicate-agent entries down to summary fields only (retains the latest full
    finding per agent, strips evidence/trace from earlier ones).
    """
    # Pass 1: hard cap
    if len(context.agent_findings) > _MAX_FINDINGS:
        dropped = len(context.agent_findings) - _MAX_FINDINGS
        logger.warning(
            "_trim_context_if_needed: agent_findings hit hard cap (%d); "
            "dropping %d oldest entries",
            _MAX_FINDINGS, dropped,
        )
        context.agent_findings = context.agent_findings[-_MAX_FINDINGS:]

    # Pass 2: size trim
    serialised = json.dumps(context.model_dump(mode="json"))
    if len(serialised) <= 6000:
        return

    seen_agents: set[str] = set()
    trimmed = []

    for finding in reversed(context.agent_findings):
        if finding.agent_id not in seen_agents:
            seen_agents.add(finding.agent_id)
            trimmed.append(finding)
        else:
            summary = AgentFinding(
                agent_id=finding.agent_id,
                dispatched_at=finding.dispatched_at,
                completed_at=finding.completed_at,
                status=finding.status,
                root_cause_hypothesis=finding.root_cause_hypothesis,
                confidence=finding.confidence,
                evidence=finding.evidence[:2],
                reasoning_trace=finding.reasoning_trace[:2],
            )
            trimmed.append(summary)

    context.agent_findings = list(reversed(trimmed))


# ── IncidentContext initialisation ─────────────────────────────────────────────

def initialise_incident_context(
    state: Any,
    trigger_signals: list[dict],
) -> IncidentContext:
    """
    Called by orchestrator at the start of a new incident.
    Creates a fresh IncidentContext and writes it to session state.
    """
    signals = [_parse_signal(s) for s in trigger_signals]

    context = IncidentContext(initial_signals=signals)
    set_incident_context(state, context)
    return context


def _parse_signal(raw: dict):
    """Parse a raw signal dict into the appropriate Signal type."""
    from models import (
        SyncStatusSignal, SchemaDiffSignal, TransformationEventSignal,
        DataQualityMetricSignal, RowCountDeltaSignal
    )
    stype = raw.get("signal_type")
    dispatch = {
        "sync_status": SyncStatusSignal,
        "schema_diff": SchemaDiffSignal,
        "transformation_event": TransformationEventSignal,
        "data_quality_metric": DataQualityMetricSignal,
        "row_count_delta": RowCountDeltaSignal,
    }
    cls = dispatch.get(stype)
    if cls is None:
        raise ValueError(f"Unknown signal_type: {stype}")
    return cls.model_validate(raw)
