from __future__ import annotations

import os
from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types

from orchestrator.prompts import ORCHESTRATOR_INSTRUCTIONS
from orchestrator.classifier import score_layers, classify
from integration.mcp_toolset import get_orchestrator_toolset, get_sub_agent_toolset, get_vertex_extra_headers

# Sub-agents for diagnosis phase (direct dispatch by orchestrator)
from agents.connector.agent import connector_agent
from agents.schema.agent import schema_agent
from agents.transformation.agent import transformation_agent, TRANSFORMATION_TOOL_FILTER
from agents.data_quality.agent import data_quality_agent, DATA_QUALITY_TOOL_FILTER
from agents.transformation.prompts import TRANSFORMATION_INSTRUCTIONS
from agents.data_quality.prompts import DATA_QUALITY_INSTRUCTIONS
from shared.context import after_agent_callback, after_agent_callback_with_loop_exit
from shared import gate_bridge as _gate_bridge


_DISPATCH_COUNT_KEY   = "_agent_dispatch_counts"
_MAX_AGENT_DISPATCHES = 2

_DISPATCHABLE_AGENTS = {
    "connector_agent", "schema_agent",
    "transformation_agent", "data_quality_agent",
}


async def request_approval(
    gate_id: str,
    tool_to_call: str,
    parameters: dict,
    current_state: str,
    proposed_action: str,
    expected_outcome: str,
    risk_assessment: str,
    alternative_if_denied: str | None,
    tool_context: ToolContext,
) -> dict:
    """
    HITL gate — truly blocking.

    ADK supports async tool functions. This awaits gate_queue.get() directly,
    suspending runner.run_async() until the human decides. The model receives
    {"approved": true/false} in the same turn and acts on it immediately.

    This eliminates the multi-tool-batch race where request_approval returned
    {"status":"pending"} and the model called create_connect_card before the
    LongRunningFunctionTool mechanism could pause the runner (D-19, four regressions).
    """
    session_id = tool_context.state.get("_pre_session_id", "")
    bridge = _gate_bridge.get(session_id)

    if bridge is None:
        # CLI / offline fallback — no bridge registered
        return {"status": "pending", "gate_id": gate_id}

    gate_data = {
        "gate_id": gate_id,
        "tool_to_call": tool_to_call,
        "parameters": parameters,
        "current_state": current_state,
        "proposed_action": proposed_action,
        "expected_outcome": expected_outcome,
        "risk_assessment": risk_assessment,
        "alternative_if_denied": alternative_if_denied,
    }

    await bridge["emit"]("gate_open", gate_data)
    approved: bool = await bridge["gate_queue"].get()
    await bridge["emit"]("gate_closed", {"approved": approved, "tool": tool_to_call})

    return {
        "gate_id": gate_id,
        "approved": approved,
        "status": "approved" if approved else "denied",
        "decided_by": "human_operator",
    }


def _orchestrator_before_tool(tool, args, tool_context) -> dict | None:
    """
    before_tool_callback for the orchestrator.

    Guard 1 (HITL gate enforcement) is removed — request_approval is now an async
    function that truly blocks until the human decides. The multi-tool-batch race
    is eliminated architecturally; no callback tricks needed (D-19 fifth fix).

    Guard 2 — per-agent dispatch cap:
      Prevents the orchestrator dispatching the same diagnostic agent more than
      _MAX_AGENT_DISPATCHES times. Forces close_incident on reasoning loops.
    """
    tool_name = getattr(tool, "name", "") or ""

    if tool_name in _DISPATCHABLE_AGENTS:
        counts: dict = tool_context.state.get(_DISPATCH_COUNT_KEY) or {}
        n = counts.get(tool_name, 0)
        if n >= _MAX_AGENT_DISPATCHES:
            return {
                "error": True,
                "dispatch_cap_reached": True,
                "reason": (
                    f"'{tool_name}' has already been dispatched {n} time(s) this incident. "
                    f"Dispatching the same agent more than {_MAX_AGENT_DISPATCHES} times indicates "
                    "a reasoning loop. Do NOT dispatch it again. "
                    "If all agents have escalated, call `close_incident` now with status='escalated'."
                ),
            }
        counts[tool_name] = n + 1
        tool_context.state[_DISPATCH_COUNT_KEY] = counts

    return None


def _normalize_blast_radius(br: dict) -> dict:
    """
    Translate whatever the LLM sends into a valid BlastRadius dict.
    LLMs often invent field names (connections_total, num_connections, etc.)
    instead of the exact model fields. This maps common variants and fills defaults.
    """
    b = dict(br)

    # Normalize connections_affected
    if "connections_affected" not in b:
        total = int(b.pop("connections_total", b.pop("num_connections", b.pop("affected_connections", 1))))
        b["connections_affected"] = [f"connection_{i+1}" for i in range(total)]
    b.pop("connections_total", None)
    b.pop("num_connections", None)
    b.pop("affected_connections", None)

    # Normalize transformations_at_risk
    if "transformations_at_risk" not in b:
        total = int(b.pop("transformations_total", b.pop("num_transformations", b.pop("at_risk_transformations", 0))))
        b["transformations_at_risk"] = [f"transformation_{i+1}" for i in range(total)]
    b.pop("transformations_total", None)
    b.pop("num_transformations", None)
    b.pop("at_risk_transformations", None)

    # Compute score from rubric if missing
    if "score" not in b:
        nc = len(b.get("connections_affected", []))
        nt = len(b.get("transformations_at_risk", []))
        conn_pts = 0 if nc <= 1 else 1 if nc <= 3 else 2 if nc <= 9 else 3
        trans_pts = 0 if nt == 0 else 1 if nt <= 2 else 2 if nt <= 5 else 3
        b["score"] = conn_pts + trans_pts

    # Derive severity from score if missing
    if "severity" not in b:
        s = b["score"]
        b["severity"] = "low" if s <= 2 else "medium" if s <= 4 else "high" if s <= 6 else "critical"

    b.setdefault("estimated_data_staleness_hours", 0.0)
    b.setdefault("assessment_reasoning", "Blast radius assessed from connection and transformation counts.")

    return b


def update_incident_context(
    classification: dict | None = None,
    blast_radius: dict | None = None,
    increment_iteration: bool = False,
    resolved: bool | None = None,
    escalated: bool | None = None,
    resolution_summary: str | None = None,
    escalation_reason: str | None = None,
    tool_context: ToolContext = None,
) -> dict:
    """
    Write orchestrator-owned fields to IncidentContext during an incident turn.
    Call this after scoring (to write classification), after blast radius assessment
    (to write blast_radius), and before each validation loop pass (increment_iteration=True).
    """
    from shared.context import get_incident_context, set_incident_context
    from models import Classification, BlastRadius

    context = get_incident_context(tool_context.state)

    if classification is not None:
        # Normalize keys from classify() output → Classification model field names.
        # The LLM may pass the raw classify() result: {layer, confidence, flag}.
        # Classification expects: {classified_layer, confidence_flag, layer_scores, reasoning}.
        c = dict(classification)
        if "layer" in c and "classified_layer" not in c:
            c["classified_layer"] = c.pop("layer")
        if "flag" in c and "confidence_flag" not in c:
            c["confidence_flag"] = c.pop("flag")
        # Normalise confidence_flag to uppercase — LLM sometimes returns "medium", "low" etc.
        if "confidence_flag" in c and isinstance(c["confidence_flag"], str):
            c["confidence_flag"] = c["confidence_flag"].upper()
        # Normalise classified_layer to lowercase
        if "classified_layer" in c and isinstance(c["classified_layer"], str):
            c["classified_layer"] = c["classified_layer"].lower()
        c.setdefault("layer_scores", {})
        c.setdefault("reasoning", "Classified by deterministic scorer.")
        context.classification = Classification.model_validate(c)
    if blast_radius is not None:
        context.blast_radius = BlastRadius.model_validate(_normalize_blast_radius(blast_radius))
    if increment_iteration:
        context.iteration_count += 1
    if resolved is not None:
        context.resolved = resolved
    if escalated is not None:
        context.escalated = escalated
    if resolution_summary is not None:
        context.resolution_summary = resolution_summary
    if escalation_reason is not None:
        context.escalation_reason = escalation_reason

    set_incident_context(tool_context.state, context)
    return {"status": "updated", "iteration_count": context.iteration_count}


INCIDENT_SUMMARY_KEY = "incident_summary"


def close_incident(
    status: str,
    root_cause_layer: str,
    root_cause_hypothesis: str,
    confidence: float,
    actions_taken: list[str],
    actions_pending: list[str],
    recovery_confirmed: bool,
    total_duration_seconds: float,
    reasoning_trace: list[str],
    resolution_summary: str | None = None,
    escalation_reason: str | None = None,
    tool_context: ToolContext = None,
) -> dict:
    """
    Close the incident and produce a validated IncidentSummary.
    Call this as the FINAL action — after resolution or escalation — instead of
    returning free text. Stores the summary in session state so main.py can
    retrieve it reliably without parsing LLM text.

    status: 'resolved' | 'escalated' | 'partial'
    """
    from shared.context import get_incident_context, set_incident_context
    from models import IncidentSummary

    context = get_incident_context(tool_context.state)

    summary = IncidentSummary(
        incident_id=context.incident_id,
        status=status,
        root_cause_layer=root_cause_layer,
        root_cause_hypothesis=root_cause_hypothesis,
        confidence=confidence,
        actions_taken=actions_taken,
        actions_pending=actions_pending,
        recovery_confirmed=recovery_confirmed,
        resolution_summary=resolution_summary,
        escalation_reason=escalation_reason,
        total_duration_seconds=total_duration_seconds,
        reasoning_trace=reasoning_trace,
    )

    context.resolved = (status == "resolved")
    context.escalated = (status in ("escalated", "partial"))
    if resolution_summary:
        context.resolution_summary = resolution_summary
    if escalation_reason:
        context.escalation_reason = escalation_reason
    set_incident_context(tool_context.state, context)

    result = summary.model_dump(mode="json")
    tool_context.state[INCIDENT_SUMMARY_KEY] = result
    return result


def get_latest_findings(tool_context: ToolContext) -> dict:
    """
    Return the most recent AgentFinding per agent from IncidentContext, plus a
    flat list of all actions_queued_for_approval across those findings.

    Call this immediately after transfer_to_agent returns to read what the
    sub-agent found and which gates it queued for approval.
    """
    from shared.context import get_incident_context

    context = get_incident_context(tool_context.state)

    seen: dict[str, dict] = {}
    for finding in reversed(context.agent_findings):
        if finding.agent_id not in seen:
            seen[finding.agent_id] = finding.model_dump(mode="json")

    queued_gates = [
        gate
        for f in seen.values()
        for gate in f.get("actions_queued_for_approval", [])
    ]

    return {
        "findings": list(seen.values()),
        "actions_queued_for_approval": queued_gates,
        "has_pending_gates": len(queued_gates) > 0,
        "iteration_count": context.iteration_count,
        "resolved": context.resolved,
        "escalated": context.escalated,
    }


# Post-fix validation loop — uses SEPARATE agent instances to avoid duplicate registration.
# transformation_agent and data_quality_agent are already direct sub_agents of the orchestrator
# for the diagnosis phase. ADK behavior is undefined when the same Python object appears in
# two sub_agents lists, so the loop uses dedicated instances with the same instructions.

transformation_runner = LlmAgent(
    name="transformation_runner",
    model=os.getenv("SUB_AGENT_MODEL", "gemini-3.1-flash-lite"),
    description="Post-fix re-run agent: confirms upstream fix, queues sync_connection and run_transformation approvals.",
    instruction=TRANSFORMATION_INSTRUCTIONS,
    tools=[get_sub_agent_toolset(TRANSFORMATION_TOOL_FILTER)],
    disallow_transfer_to_peers=True,
    after_agent_callback=after_agent_callback,
    output_key="transformation_runner_output",
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)

dq_validator = LlmAgent(
    name="dq_validator",
    model=os.getenv("SUB_AGENT_MODEL", "gemini-3.1-flash-lite"),
    description="Post-fix validation judge: confirms recovery_confirmed; signals LoopAgent exit when True.",
    instruction=DATA_QUALITY_INSTRUCTIONS,
    tools=[get_sub_agent_toolset(DATA_QUALITY_TOOL_FILTER)],
    disallow_transfer_to_peers=True,
    after_agent_callback=after_agent_callback_with_loop_exit,
    output_key="dq_validator_output",
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)

post_fix_validation = LoopAgent(
    name="post_fix_validation",
    sub_agents=[
        transformation_runner,  # triggers sync + run_transformation (both gated)
        dq_validator,           # validates recovery; escalates when confirmed
    ],
    max_iterations=3,
)

# Wrap diagnosis sub-agents as AgentTools so the orchestrator receives their
# output as a tool response and continues its turn (can then call get_latest_findings
# and request_approval in the same LLM context). Using transfer_to_agent / sub_agents
# ends the orchestrator turn permanently — the orchestrator never gets to act on findings.
# post_fix_validation stays as a sub_agent (LoopAgent, not a direct dispatch target).

orchestrator_agent = LlmAgent(
    name="pre_orchestrator",
    model=os.getenv("ORCHESTRATOR_MODEL", "gemini-3.1-flash-lite"),
    description=(
        "Pipeline Reliability Engineer orchestrator. Classifies Fivetran pipeline failures "
        "by layer, dispatches specialised sub-agents, manages human approval gates, "
        "and validates recovery."
    ),
    instruction=ORCHESTRATOR_INSTRUCTIONS,
    tools=[
        score_layers,                                       # deterministic classifier tool
        classify,                                           # layer selection with tiebreak
        update_incident_context,                            # writes classification/blast_radius/iteration_count
        get_latest_findings,                                # reads agent findings + queued gates after sub-agent runs
        close_incident,                                     # final action — produces validated IncidentSummary
        request_approval,                                   # HITL gate — async, blocks until human decides
        AgentTool(agent=connector_agent),                   # diagnosis sub-agent (returns to orchestrator)
        AgentTool(agent=schema_agent),
        AgentTool(agent=transformation_agent),
        AgentTool(agent=data_quality_agent),
        AgentTool(agent=post_fix_validation),               # LoopAgent wrapped as tool — orchestrator continues after loop exits
        get_orchestrator_toolset(),                         # write-enabled Fivetran MCP toolset
    ],
    sub_agents=[],
    before_tool_callback=_orchestrator_before_tool,   # per-agent dispatch cap
    # No after_agent_callback — orchestrator produces IncidentSummary (not AgentFinding).
    generate_content_config=genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(include_thoughts=True),
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)
