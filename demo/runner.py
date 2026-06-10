from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid as _uuid
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from orchestrator.agent import orchestrator_agent, INCIDENT_SUMMARY_KEY
from shared.context import INCIDENT_CONTEXT_KEY, _parse_signal
from shared import gate_bridge as _gate_bridge
from shared.sanitize import sanitize_trigger
from models import IncidentContext

APP_NAME = "pre_agent"
USER_ID = "operator"

_QUOTA_BACKOFF_SECONDS = [10, 30, 60]

AGENT_TOOL_NAMES = {
    "connector_agent",
    "schema_agent",
    "transformation_agent",
    "data_quality_agent",
    "post_fix_validation",
}

# Internal ADK orchestrator tools — Python functions, NOT Fivetran API calls.
# Everything else emitted as tool_call is a live Fivetran MCP tool.
INTERNAL_TOOL_NAMES = {
    "score_layers",
    "classify",
    "update_incident_context",
    "get_latest_findings",
    "request_approval",
    "close_incident",
}

EmitFn = Callable[[str, dict], Coroutine]


@dataclass
class RunMetrics:
    """Accumulates per-run metrics for the eval dashboard."""
    run_id: str
    scenario_id: str
    scenario_title: str
    connector_id: str = ""
    start_time: float = field(default_factory=time.monotonic)
    wall_start: str = field(default_factory=lambda: _dt.datetime.utcnow().isoformat())
    tokens_in: int = 0
    tokens_out: int = 0
    tool_call_count: int = 0
    hitl_gate_count: int = 0
    agent_dispatch_path: list[str] = field(default_factory=list)
    handoff_occurred: bool = False
    retry_count: int = 0
    first_event_latency_ms: int | None = None
    error_occurred: bool = False
    monitoring_phase_ms: int = 0          # D-66: time in live_sync_and_detect
    agent_phase_ms: int = 0               # D-66: time in _streaming_agent_loop
    per_agent_duration_ms: dict = field(default_factory=dict)   # D-49
    per_agent_tokens: dict = field(default_factory=dict)        # D-46
    _current_agent: str = "orchestrator"  # D-46: attribution cursor
    _agent_start_times: dict = field(default_factory=dict)      # D-49: per-agent start times
    _fatal_tool_errors: dict = field(default_factory=dict)      # tool_name → consecutive failure count
    _force_break: bool = False                                   # set True to exit run_async loop early
    _active_spans: dict = field(default_factory=dict)           # D-45: name → active OTel span

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.start_time) * 1000)


async def run_incident_streaming(
    trigger_payload: dict,
    emit: EmitFn,
    gate_queue: asyncio.Queue,
    run_id: str = "",
    scenario_id: str = "",
    scenario_title: str = "",
    connector_id: str = "",
    monitoring_phase_ms: int = 0,
) -> dict:
    """
    Run a full incident lifecycle, emitting SSE events via `emit` and pausing
    at HITL gates until `gate_queue` provides a bool decision.
    Persists run metrics to the eval DB on completion.
    """
    trigger_payload = sanitize_trigger(trigger_payload)
    signals = [_parse_signal(s) for s in trigger_payload.get("signals", [])]
    context = IncidentContext(initial_signals=signals)

    # Generate bridge_key BEFORE creating the session so it can be embedded in
    # initial_state. ADK's tool_context.state only reflects initial_state at
    # creation time — post-creation mutations to session.state are not visible
    # inside tool functions. If we set _pre_session_id after create_session,
    # request_approval gets "" from tool_context.state, bridge lookup returns None,
    # and the function falls back to {"status":"pending"} (non-blocking).
    bridge_key = run_id or str(_uuid.uuid4())[:8]

    initial_state = {
        INCIDENT_CONTEXT_KEY: context.model_dump(mode="json"),
        "_pre_session_id": bridge_key,  # readable by request_approval via tool_context.state
    }

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

    message = types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(trigger_payload, indent=2))],
    )

    metrics = RunMetrics(
        run_id=bridge_key,
        scenario_id=scenario_id,
        scenario_title=scenario_title,
        connector_id=connector_id,
        monitoring_phase_ms=monitoring_phase_ms,
    )

    # Wrap emit to count gate_open events for metrics, then register the bridge.
    # request_approval awaits gate_queue.get() via this bridge — truly blocking.
    _base_emit = emit

    async def _tracking_emit(event_type: str, data: dict) -> None:
        if event_type == "gate_open":
            metrics.hitl_gate_count += 1
        await _base_emit(event_type, data)

    _gate_bridge.register(bridge_key, _tracking_emit, gate_queue)

    # D-45: root span for the entire run — child agent spans nest under it
    _run_span = _span_start(f"pre_run/{bridge_key}", {
        "scenario_id": scenario_id,
        "connector_id": connector_id,
    })

    await _tracking_emit("status", {"message": "Incident response starting…"})

    timeout = int(os.getenv("RUN_TIMEOUT_SECONDS", "360"))
    _agent_start = time.monotonic()
    result = None  # initialise so finally can always reference it
    try:
        result = await asyncio.wait_for(
            _streaming_agent_loop(
                runner=runner,
                session_id=session.id,
                session_service=session_service,
                message=message,
                emit=_tracking_emit,
                metrics=metrics,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        metrics.error_occurred = True
        result = {
            "incident_id": str(_uuid.uuid4()),
            "status": "escalated",
            "root_cause_layer": "unknown",
            "root_cause_hypothesis": "Incident response exceeded the maximum allowed time.",
            "confidence": 0.0,
            "actions_taken": [],
            "actions_pending": ["Manual investigation required — agent run timed out."],
            "recovery_confirmed": False,
            "total_duration_seconds": timeout,
            "reasoning_trace": [f"Run exceeded {timeout}s wall-clock timeout and was terminated."],
            "escalation_reason": f"Agent run did not complete within {timeout}s.",
            "created_at": _dt.datetime.utcnow().isoformat(),
        }
        await _tracking_emit("result", result)
    except BaseException as _exc:
        # Non-timeout exception from _streaming_agent_loop (e.g. unhandled API error).
        # Build a minimal result so _persist_metrics can still record the run, then re-raise.
        if not isinstance(_exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
            metrics.error_occurred = True
            if result is None:
                result = {
                    "incident_id": str(_uuid.uuid4()),
                    "status": "escalated",
                    "root_cause_layer": "unknown",
                    "root_cause_hypothesis": "Agent loop raised an unexpected exception.",
                    "confidence": 0.0,
                    "actions_taken": [],
                    "actions_pending": ["Manual investigation required."],
                    "recovery_confirmed": False,
                    "total_duration_seconds": 0,
                    "reasoning_trace": [str(_exc)[:200]],
                    "escalation_reason": f"Unexpected error: {type(_exc).__name__}: {str(_exc)[:120]}",
                    "created_at": _dt.datetime.utcnow().isoformat(),
                }
        raise
    finally:
        metrics.agent_phase_ms = int((time.monotonic() - _agent_start) * 1000)
        _gate_bridge.unregister(bridge_key)
        # D-45: close root span and any leaked agent spans
        for span in metrics._active_spans.values():
            _span_end(span, error=True)
        metrics._active_spans.clear()
        _span_end(_run_span, error=metrics.error_occurred)
        # Persist regardless of how the run ended — result is always set here
        if result is not None:
            _persist_metrics(metrics, result)

    return result


def _persist_metrics(metrics: RunMetrics, result: dict) -> None:
    """Write run metrics to the eval DB, then check P95 baselines. Best-effort."""
    try:
        from demo.db import insert_run, get_baselines
        import logging as _dblog
        _dblog.getLogger(__name__).info("persist_metrics: writing run %s to eval DB", metrics.run_id)
        row = {
            "run_id":                metrics.run_id,
            "scenario_id":           metrics.scenario_id,
            "scenario_title":        metrics.scenario_title,
            "connector_id":          metrics.connector_id,
            "timestamp":             metrics.wall_start,
            "duration_ms":           metrics.elapsed_ms(),
            "tokens_in":             metrics.tokens_in,
            "tokens_out":            metrics.tokens_out,
            "root_cause_layer":      result.get("root_cause_layer"),
            "status":                result.get("status"),
            "confidence":            result.get("confidence"),
            "recovery_confirmed":    result.get("recovery_confirmed", False),
            "agent_dispatch_path":   metrics.agent_dispatch_path,
            "tool_call_count":       metrics.tool_call_count,
            "hitl_gate_count":       metrics.hitl_gate_count,
            "handoff_occurred":      metrics.handoff_occurred,
            "retry_count":            metrics.retry_count,
            "first_event_latency_ms": metrics.first_event_latency_ms,
            "error_occurred":         metrics.error_occurred,
            "monitoring_phase_ms":    metrics.monitoring_phase_ms,
            "agent_phase_ms":         metrics.agent_phase_ms,
            "otps": round(metrics.tokens_out / (metrics.agent_phase_ms / 1000), 2)
                    if metrics.agent_phase_ms > 0 else None,
            "per_agent_duration_ms":  metrics.per_agent_duration_ms,
            "per_agent_tokens":       metrics.per_agent_tokens,
        }
        insert_run(row)
        _check_baselines(row)
    except Exception as _e:
        import logging as _dblog
        _dblog.getLogger(__name__).error("persist_metrics FAILED for run %s: %s: %s",
                                          metrics.run_id, type(_e).__name__, str(_e)[:300])


_OUTPUT_TOKEN_BUDGET = int(os.getenv("OUTPUT_TOKEN_BUDGET", "8000"))


def _check_baselines(row: dict) -> None:
    """Log a warning if this run exceeds 2×P95 for duration or cost, or blows the token budget."""
    try:
        import logging
        _log = logging.getLogger(__name__)

        # D-47: token budget check — independent of baseline readiness
        tokens_out = row.get("tokens_out") or 0
        if _OUTPUT_TOKEN_BUDGET and tokens_out > _OUTPUT_TOKEN_BUDGET:
            _log.warning(
                "PRE token budget alert: run %s output %d tokens exceeds budget of %d — "
                "possible verbose reasoning chain or unexpected agent loop",
                row["run_id"], tokens_out, _OUTPUT_TOKEN_BUDGET,
            )

        from demo.db import get_baselines
        baselines = get_baselines()
        if baselines is None:
            return  # not enough data yet

        duration_ms = row.get("duration_ms") or 0
        cost        = row.get("estimated_cost_usd") or 0.0
        p95_dur     = baselines["p95_duration_ms"] or 0
        p95_cost    = baselines["p95_cost_usd"] or 0.0

        if p95_dur and duration_ms > p95_dur * 2:
            _log.warning(
                "PRE baseline alert: run %s duration %dms exceeds 2×P95 (%dms) — "
                "possible model latency spike or cascading retries",
                row["run_id"], duration_ms, p95_dur,
            )
        if p95_cost and cost > p95_cost * 2:
            _log.warning(
                "PRE baseline alert: run %s cost $%.4f exceeds 2×P95 ($%.4f) — "
                "possible token budget overrun or unusually long reasoning chain",
                row["run_id"], cost, p95_cost,
            )
    except Exception:
        pass


def _is_quota_error(exc: BaseException) -> bool:
    if hasattr(exc, "exceptions"):
        return any(_is_quota_error(e) for e in exc.exceptions)
    msg = str(exc)
    return (
        "429" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "quota" in msg.lower()
        or type(exc).__name__ in ("_ResourceExhaustedError", "ResourceExhausted")
    )


async def _streaming_agent_loop(
    runner: Runner,
    session_id: str,
    session_service: InMemorySessionService,
    message: types.Content,
    emit: EmitFn,
    metrics: RunMetrics,
) -> dict:
    """
    Run the orchestrator agent to completion, handling quota retries.

    HITL gates are handled inside request_approval (an async function that directly
    awaits gate_queue.get()). There is no gate-detection or outer-loop continuation
    here — runner.run_async() suspends mid-iteration while the human decides, then
    resumes when gate_queue.put() is called by the /api/gate/respond endpoint.
    """
    last_text = ""

    for attempt, backoff in enumerate([0] + _QUOTA_BACKOFF_SECONDS):
        if backoff:
            metrics.retry_count += 1
            await emit("status", {
                "message": f"Rate limited — waiting {backoff}s (retry {attempt}/{len(_QUOTA_BACKOFF_SECONDS)})…",
                "level": "warning",
            })
            await asyncio.sleep(backoff)

        last_text = ""

        try:
            async for event in runner.run_async(
                session_id=session_id,
                user_id=USER_ID,
                new_message=message,
            ):
                if metrics.first_event_latency_ms is None:
                    if event.content and event.content.parts:
                        metrics.first_event_latency_ms = metrics.elapsed_ms()

                um = getattr(event, "usage_metadata", None)
                if um:
                    metrics.tokens_in  += getattr(um, "prompt_token_count", 0) or 0
                    metrics.tokens_out += getattr(um, "candidates_token_count", 0) or 0

                await _emit_adk_event(event, emit, metrics)

                if metrics._force_break:
                    break  # fatal tool error threshold reached — D-34 fallback handles escalation

                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            last_text = part.text
            break  # run_async completed — no quota error

        except BaseException as exc:
            if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                raise
            if _is_quota_error(exc) and attempt < len(_QUOTA_BACKOFF_SECONDS):
                continue
            metrics.error_occurred = True
            await emit("error", {"message": str(exc)})
            raise

    # ── Done — outside the retry loop ────────────────────────────────────
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if INCIDENT_SUMMARY_KEY in session.state:
        final_output = session.state[INCIDENT_SUMMARY_KEY]
    else:
        # Orchestrator didn't call close_incident — try to parse text output,
        # then synthesise a minimal escalation result so the run is never status=None.
        stripped = _strip_fences(last_text)
        try:
            final_output = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "run %s: orchestrator did not call close_incident — synthesising fallback result",
                metrics.run_id,
            )
            ctx_raw = session.state.get(INCIDENT_CONTEXT_KEY) or {}
            findings = ctx_raw.get("agent_findings", []) if isinstance(ctx_raw, dict) else []
            layer = findings[-1].get("agent_id") if findings else "unknown"
            final_output = {
                "incident_id": str(_uuid.uuid4()),
                "status": "escalated",
                "root_cause_layer": layer,
                "root_cause_hypothesis": "Orchestrator did not produce a structured result — manual investigation required.",
                "confidence": 0.0,
                "actions_taken": [],
                "actions_pending": ["Review agent findings and close incident manually."],
                "recovery_confirmed": False,
                "total_duration_seconds": metrics.elapsed_ms() / 1000,
                "reasoning_trace": [last_text[:300]] if last_text else [],
                "escalation_reason": "close_incident was not called — orchestrator produced unstructured output.",
                "created_at": _dt.datetime.utcnow().isoformat(),
            }

    await emit("result", final_output)
    return final_output


def _span_start(name: str, attributes: dict | None = None) -> Any:
    """D-45: Start an OTel span. Returns None if OTel is not configured (graceful no-op)."""
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("pre_agent")
        span = tracer.start_span(name)
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        return span
    except Exception:
        return None


def _span_end(span: Any, error: bool = False) -> None:
    """D-45: End an OTel span. Safe to call with None."""
    try:
        if span is None:
            return
        if error:
            from opentelemetry.trace import StatusCode
            span.set_status(StatusCode.ERROR)
        span.end()
    except Exception:
        pass


async def _emit_adk_event(event: Any, emit: EmitFn, metrics: RunMetrics) -> None:
    # D-46: attribute usage_metadata tokens to the currently active agent
    um = getattr(event, "usage_metadata", None)
    if um:
        out_tokens = getattr(um, "candidates_token_count", 0) or 0
        if out_tokens:
            agent_key = metrics._current_agent
            metrics.per_agent_tokens[agent_key] = metrics.per_agent_tokens.get(agent_key, 0) + out_tokens

    if not event.content or not event.content.parts:
        return

    for part in event.content.parts:
        if hasattr(part, "thought") and part.thought:
            text = part.text or ""
            await emit("thinking", {
                "text": text[:600] + ("…" if len(text) > 600 else "")
            })
        elif part.text:
            await emit("text", {"text": part.text})
        elif part.function_call:
            name = part.function_call.name
            args = part.function_call.args or {}
            if name in AGENT_TOOL_NAMES:
                metrics.agent_dispatch_path.append(name)
                metrics._agent_start_times[name] = time.monotonic()  # D-49
                metrics._current_agent = name                         # D-46
                metrics._active_spans[name] = _span_start(           # D-45
                    f"agent/{name}", {"run_id": metrics.run_id}
                )
                await emit("agent_dispatch", {"agent": name})
            else:
                metrics.tool_call_count += 1
                # Detect handoffs: if the orchestrator dispatches a secondary agent
                # after the primary one, a handoff occurred
                if len(metrics.agent_dispatch_path) > 1:
                    metrics.handoff_occurred = True
                await emit("tool_call", {
                    "name": name,
                    "args_summary": _summarise_args(args),
                    "args_full": args,
                    "is_fivetran": name not in INTERNAL_TOOL_NAMES,
                })
        elif part.function_response:
            name = part.function_response.name
            resp = part.function_response.response
            if name in AGENT_TOOL_NAMES:
                # D-49: record agent duration on completion
                start = metrics._agent_start_times.get(name)
                if start:
                    metrics.per_agent_duration_ms[name] = int((time.monotonic() - start) * 1000)
                metrics._current_agent = "orchestrator"              # D-46: return attribution
                _span_end(metrics._active_spans.pop(name, None))     # D-45: close agent span
                await emit("agent_complete", {"agent": name})
            else:
                resp_str = ""
                if resp:
                    try:
                        resp_str = json.dumps(resp, default=str)[:400]
                    except Exception:
                        resp_str = str(resp)[:400]
                await emit("tool_response", {
                    "name": name,
                    "response": resp_str,
                })
                # Detect fatal tool errors (hallucinated tool names, etc.)
                # If the same tool returns a fatal/not-found error 3 times, force-break
                # the run_async loop and let the D-34 fallback produce a structured escalation.
                _is_fatal = False
                if isinstance(resp, dict):
                    _is_fatal = bool(resp.get("fatal")) or resp.get("code") == "TOOL_NOT_FOUND"
                elif isinstance(resp_str, str):
                    _is_fatal = '"fatal": true' in resp_str or "TOOL_NOT_FOUND" in resp_str
                if _is_fatal:
                    metrics._fatal_tool_errors[name] = metrics._fatal_tool_errors.get(name, 0) + 1
                    if metrics._fatal_tool_errors[name] >= 3:
                        import logging as _rlog
                        _rlog.getLogger(__name__).warning(
                            "run %s: tool '%s' returned fatal error %d times — forcing break",
                            metrics.run_id, name, metrics._fatal_tool_errors[name],
                        )
                        await emit("status", {
                            "message": f"Tool '{name}' unavailable after 3 attempts — escalating run.",
                            "level": "warning",
                        })
                        metrics._force_break = True
                # D-61: extract Connect Card URL so result card can surface it as a link
                if name == "create_connect_card" and resp:
                    import logging as _rlog
                    _rlog.getLogger(__name__).info("create_connect_card resp type=%s keys=%s",
                        type(resp).__name__,
                        list(resp.keys()) if isinstance(resp, dict) else repr(resp)[:200])
                    url = _extract_connect_card_url(resp)
                    if url:
                        await emit("connect_card_url", {"url": url})
                    else:
                        _rlog.getLogger(__name__).warning("create_connect_card: URL not found in resp: %s", str(resp)[:400])


def _extract_connect_card_url(resp: Any) -> str | None:
    """D-61: Extract Connect Card URI from Fivetran create_connect_card response.

    ADK MCPToolset surfaces MCP results as:
      {"content": [{"type": "text", "text": "<fivetran json string>"}]}
    The Fivetran JSON inside text is:
      {"data": {"connect_card": {"uri": "https://..."}}}
    Also handles direct dict and other wrapper shapes as fallback.
    """
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except (json.JSONDecodeError, TypeError):
            return None

    if not isinstance(resp, dict):
        return None

    # Primary path: MCP protocol content envelope
    content = resp.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    url = _extract_connect_card_url(parsed)
                    if url:
                        return url
                except (json.JSONDecodeError, TypeError):
                    pass

    # Fallback: direct Fivetran response or other wrapper keys
    candidates = [resp]
    for wrapper_key in ("data", "result", "output"):
        inner = resp.get(wrapper_key)
        if isinstance(inner, dict):
            candidates.append(inner)
        elif isinstance(inner, str):
            try:
                parsed = json.loads(inner)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
                    for k in ("data", "result", "output"):
                        deep = parsed.get(k)
                        if isinstance(deep, dict):
                            candidates.append(deep)
            except (json.JSONDecodeError, TypeError):
                pass

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        cc = candidate.get("connect_card", {})
        if isinstance(cc, dict) and cc.get("uri"):
            return cc["uri"]
        if candidate.get("uri"):
            return candidate["uri"]

    return None


def _summarise_args(args: dict | None) -> str:
    if not args:
        return ""
    keys = list(args.keys())
    if len(keys) <= 2:
        return ", ".join(f"{k}={repr(v)[:60]}" for k, v in args.items())
    return f"{keys[0]}=…, ({len(keys)} args)"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()
    return text
