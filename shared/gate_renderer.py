from __future__ import annotations

import json
from typing import Any

from google.genai import types


# ── Card rendering ─────────────────────────────────────────────────────────────

def render_gate_card(gate: dict) -> str:
    """
    Format a human-readable approval card for terminal display.
    Input: gate dict extracted from a LongRunningFunctionTool call's arguments.
    """
    tool = gate.get("tool_to_call", "UNKNOWN TOOL")
    agent = gate.get("agent_id", "unknown agent")
    params = gate.get("parameters", {})
    current_state = gate.get("current_state", "Not provided")
    proposed_action = gate.get("proposed_action", "Not provided")
    expected_outcome = gate.get("expected_outcome", "Not provided")
    risk = gate.get("risk_assessment", "Not provided")
    alternative = gate.get("alternative_if_denied") or "Escalate to human operator"
    gate_id = gate.get("gate_id", "unknown")

    params_str = json.dumps(params, indent=4)

    return f"""
╔══════════════════════════════════════════════════════════════════╗
║  APPROVAL REQUIRED
║  Agent : {agent:<50} ║
║  Tool  : {tool:<50} ║
║  Gate  : {gate_id:<50} ║
╠══════════════════════════════════════════════════════════════════╣
║  CURRENT STATE:
{_wrap_lines(current_state, width=64, prefix='║    ')}
║
║  PROPOSED ACTION:
║    Tool:       {tool}
║    Parameters:
{_indent_json(params_str, prefix='║      ')}
║
║  EXPECTED OUTCOME:
{_wrap_lines(expected_outcome, width=64, prefix='║    ')}
║
║  RISK:
{_wrap_lines(risk, width=64, prefix='║    ')}
║
║  IF DENIED:
{_wrap_lines(alternative, width=64, prefix='║    ')}
╚══════════════════════════════════════════════════════════════════╝"""


def _wrap_lines(text: str, width: int, prefix: str) -> str:
    """Wrap long text into lines with a consistent prefix."""
    words = text.split()
    lines = []
    current = prefix
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = prefix + word
        else:
            current = current + (" " if current != prefix else "") + word
    if current != prefix:
        lines.append(current)
    return "\n".join(lines) if lines else prefix


def _indent_json(json_str: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in json_str.splitlines())


# ── Gate extraction from ADK events ───────────────────────────────────────────

def extract_gate_from_event(
    event: Any,
    long_running_call: types.FunctionCall,
) -> dict:
    """
    Extract gate context from a LongRunningFunctionTool call's arguments.
    Returns a dict with all gate fields for rendering and response building.
    """
    args = long_running_call.args or {}
    return {
        "gate_id": args.get("gate_id", ""),
        "tool_to_call": args.get("tool_to_call", ""),
        "parameters": args.get("parameters", {}),
        "current_state": args.get("current_state", ""),
        "proposed_action": args.get("proposed_action", ""),
        "expected_outcome": args.get("expected_outcome", ""),
        "risk_assessment": args.get("risk_assessment", ""),
        "alternative_if_denied": args.get("alternative_if_denied"),
        "function_call_id": long_running_call.id,
        "function_call_name": long_running_call.name,
    }


# ── Approval response builder ──────────────────────────────────────────────────

def build_approval_response(gate: dict, approved: bool) -> types.Content:
    """
    Build the FunctionResponse content to re-inject into runner.run_async().
    This resumes the orchestrator agent with the human's decision.
    """
    response_data = {
        "gate_id": gate["gate_id"],
        "status": "approved" if approved else "denied",
        "decided_by": "human_operator",
    }

    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=gate["function_call_id"],
                    name=gate["function_call_name"],
                    response=response_data,
                )
            )
        ],
    )
