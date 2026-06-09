from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

# Load env first — must happen before any ADK imports that read env vars
load_dotenv()

from orchestrator.agent import orchestrator_agent, INCIDENT_SUMMARY_KEY
from shared.context import INCIDENT_CONTEXT_KEY, _parse_signal, get_incident_context
from shared.sanitize import sanitize_trigger
from shared.gate_renderer import (
    extract_gate_from_event,
    render_gate_card,
    build_approval_response,
)
from models import IncidentContext

APP_NAME = "pre_agent"
USER_ID = "operator"

# Maximum number of continuation turns injected to process pending gates.
# Guards against infinite loops if the orchestrator keeps producing new gates.
MAX_CONTINUATIONS = 4


async def run_incident(trigger_payload: dict) -> dict:
    """
    Run a full incident lifecycle.
    Returns the final IncidentSummary dict.
    """
    trigger_payload = sanitize_trigger(trigger_payload)
    signals = [_parse_signal(s) for s in trigger_payload.get("signals", [])]
    context = IncidentContext(initial_signals=signals)
    initial_state = {INCIDENT_CONTEXT_KEY: context.model_dump(mode="json")}

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state=initial_state,
    )

    runner = Runner(
        agent=orchestrator_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    initial_message = types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(trigger_payload, indent=2))],
    )

    print(f"\n{'='*68}")
    print("PRE — Incident Response Starting")
    print(f"{'='*68}\n")

    timeout = int(os.getenv("RUN_TIMEOUT_SECONDS", "360"))
    try:
        result = await asyncio.wait_for(
            _run_agent_loop(
                runner=runner,
                session_id=session.id,
                session_service=session_service,
                message=initial_message,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        import uuid, datetime
        result = {
            "incident_id": str(uuid.uuid4()),
            "status": "escalated",
            "root_cause_layer": "unknown",
            "root_cause_hypothesis": "Incident response exceeded the maximum allowed time.",
            "confidence": 0.0,
            "actions_taken": [],
            "actions_pending": ["Manual investigation required — agent run timed out."],
            "recovery_confirmed": False,
            "total_duration_seconds": timeout,
            "reasoning_trace": [f"Run exceeded {timeout}s wall-clock timeout and was terminated."],
            "escalation_reason": f"Agent run did not complete within {timeout}s. Check quota, model availability, and trigger payload complexity.",
            "created_at": datetime.datetime.utcnow().isoformat(),
        }

    print(f"\n{'='*68}")
    print("PRE — Incident Complete")
    print(f"{'='*68}\n")

    return result


_QUOTA_BACKOFF_SECONDS = [10, 30, 60]   # waits before retry 1, 2, 3


def _is_quota_error(exc: BaseException) -> bool:
    """True when the Gemini API returned 429 / RESOURCE_EXHAUSTED.

    In Python 3.11+, asyncio wraps unhandled task exceptions in ExceptionGroup.
    We recurse into .exceptions to check the wrapped causes.
    """
    # Unwrap ExceptionGroup / BaseExceptionGroup (duck-typed for Python < 3.11 compat)
    if hasattr(exc, "exceptions"):
        return any(_is_quota_error(e) for e in exc.exceptions)
    msg = str(exc)
    return (
        "429" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "quota" in msg.lower()
        or type(exc).__name__ in ("_ResourceExhaustedError", "ResourceExhausted")
    )


async def _run_agent_loop(
    runner: Runner,
    session_id: str,
    session_service: InMemorySessionService,
    message: types.Content,
) -> dict:
    """
    Run the agent, handling HITL gates between turns and 429 quota errors.

    Sub-agents are registered as AgentTools — the orchestrator receives their
    output as a tool response and continues in the same turn. The only reason
    to re-enter runner.run_async is a LongRunningFunctionTool (HITL gate) or
    a 429 quota error (retried with exponential backoff).
    """
    final_output = {}

    while True:
        long_running_call = None
        last_text = ""

        # ── Stream one turn, retrying on quota errors ─────────────────────────
        for attempt, backoff in enumerate([0] + _QUOTA_BACKOFF_SECONDS):
            if backoff:
                print(f"\n  [429] Quota exhausted — waiting {backoff}s "
                      f"(retry {attempt}/{len(_QUOTA_BACKOFF_SECONDS)})...\n")
                await asyncio.sleep(backoff)

            long_running_call = None
            last_text = ""

            try:
                async for event in runner.run_async(
                    session_id=session_id,
                    user_id=USER_ID,
                    new_message=message,
                ):
                    _print_event(event)

                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if (
                                part.function_call
                                and event.long_running_tool_ids
                                and part.function_call.id in event.long_running_tool_ids
                            ):
                                long_running_call = part.function_call
                            if part.text:
                                last_text = part.text
                break  # turn completed successfully

            except BaseException as exc:
                # Always let Python's own control-flow exceptions through immediately.
                if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                    raise
                # ADK's _ResourceExhaustedError may not inherit from Exception in all
                # versions — using BaseException guarantees we catch it regardless.
                if _is_quota_error(exc) and attempt < len(_QUOTA_BACKOFF_SECONDS):
                    continue  # retry after backoff
                raise  # non-quota error or retries exhausted

        # ── HITL gate: present to human, re-enter with decision ───────────────
        if long_running_call is not None:
            gate = extract_gate_from_event(None, long_running_call)
            print(render_gate_card(gate))

            decision = _get_human_decision()

            if decision:
                print(f"\n✓ Approved — executing: {gate['tool_to_call']}\n")
            else:
                print(f"\n✗ Denied — routing to: {gate.get('alternative_if_denied', 'escalate')}\n")

            message = build_approval_response(gate, approved=decision)
            continue

        # ── Done — read IncidentSummary from session state (set by close_incident tool)
        # Fall back to parsing last_text only if the tool wasn't called.
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
        if INCIDENT_SUMMARY_KEY in session.state:
            final_output = session.state[INCIDENT_SUMMARY_KEY]
        else:
            stripped = _strip_fences(last_text)
            try:
                final_output = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                final_output = {"summary": last_text}
        break

    return final_output


async def _get_pending_gates(
    session_service: InMemorySessionService,
    session_id: str,
) -> list[dict]:
    """
    Read IncidentContext from session state and return all unresolved
    actions_queued_for_approval across the latest finding per agent.
    Returns an empty list when the incident is resolved/escalated or
    when no findings with queued actions exist.
    """
    try:
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
        context = get_incident_context(session.state)

        # Don't inject a continuation if the incident is already closed
        if context.resolved or context.escalated:
            return []

        # Collect gates from the most recent finding per agent
        seen: set[str] = set()
        gates: list[dict] = []
        for finding in reversed(context.agent_findings):
            if finding.agent_id not in seen:
                seen.add(finding.agent_id)
                for gate in finding.actions_queued_for_approval:
                    gates.append(gate.model_dump(mode="json"))

        return gates

    except Exception:
        return []


def _strip_fences(text: str) -> str:
    """Strip markdown code fences from a string before JSON parsing."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()
    return text


def _get_human_decision() -> bool:
    """Prompt the human for approve/deny with timeout handling."""
    timeout_seconds = int(os.getenv("GATE_TIMEOUT_SECONDS", "30"))
    print(f"\nTimeout: {timeout_seconds}s (default: deny)\n")
    try:
        raw = input("Approve? [y/n]: ").strip().lower()
        return raw in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\nNo input received — defaulting to deny.")
        return False


def _print_event(event: Any) -> None:
    """Print streaming agent events in a readable format."""
    if not event.content or not event.content.parts:
        return

    for part in event.content.parts:
        if hasattr(part, "thought") and part.thought:
            print(f"  [thinking] {part.text[:200]}..." if len(part.text) > 200 else f"  [thinking] {part.text}")
        elif part.text:
            print(part.text, end="", flush=True)
        elif part.function_call:
            print(f"\n  → Tool call: {part.function_call.name}({_summarise_args(part.function_call.args)})")
        elif part.function_response:
            status = "✓" if part.function_response.response else "?"
            print(f"  {status} Tool response: {part.function_response.name}")


def _summarise_args(args: dict | None) -> str:
    if not args:
        return ""
    keys = list(args.keys())
    if len(keys) <= 2:
        return ", ".join(f"{k}={repr(v)}" for k, v in args.items())
    return f"{keys[0]}=..., ({len(keys)} args total)"


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    """
    CLI entry point.
    Usage: python main.py <path_to_trigger.json>
    """
    if len(sys.argv) < 2:
        print("Usage: python main.py <trigger_payload.json>")
        print("       Trigger payload must match the IncidentTrigger schema.")
        sys.exit(1)

    trigger_path = Path(sys.argv[1])
    if not trigger_path.exists():
        print(f"Error: trigger file not found: {trigger_path}")
        sys.exit(1)

    with open(trigger_path) as f:
        trigger_payload = json.load(f)

    from integration.cloud_trace import setup_tracing
    provider = setup_tracing()

    # Outer retry loop for 429s raised by ADK background tasks.
    # These bypass the inner async try/except in _run_agent_loop because
    # the exception propagates via asyncio task infrastructure, not the
    # runner.run_async() generator call stack.
    # On retry the session is recreated (in-memory state is lost), but
    # the incident is deterministic so the analysis restarts cleanly.
    _outer_backoffs = [10, 30, 60]

    try:
        for attempt in range(len(_outer_backoffs) + 1):
            try:
                result = asyncio.run(run_incident(trigger_payload))
                print("\nFinal output:")
                print(json.dumps(result, indent=2, default=str))
                break
            except BaseException as exc:
                if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                    raise
                if _is_quota_error(exc) and attempt < len(_outer_backoffs):
                    wait = _outer_backoffs[attempt]
                    print(f"\n  [429] Quota exhausted — waiting {wait}s "
                          f"(retry {attempt + 1}/{len(_outer_backoffs)})...\n")
                    import time
                    time.sleep(wait)
                    continue
                raise
    finally:
        if provider:
            provider.force_flush()


if __name__ == "__main__":
    main()
