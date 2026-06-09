# PRE — Pipeline Reliability Engineer

A multi-agent system that investigates, diagnoses, and fixes Fivetran data pipeline failures — with a human in the loop.

**[→ Live Demo](https://pre-agent-498009659419.us-central1.run.app)**

---

## Jump to any section:

  1. [The Problem](#1-the-problem)
  2. [Live Demo](#2-live-demo)
  3. [Architecture](#3-architecture)
  4. [Multi-Agent Pipeline](#4-multi-agent-pipeline)
  5. [Fivetran MCP Integration](#5-fivetran-mcp-integration)
  6. [Engineering Challenges](#6-engineering-challenges)
  7.  [Production Hardening](#7-production-hardening)
  8. [Eval Dashboard](#8-eval-dashboard)
  9. [Running Locally](#9-running-locally)
  10. [Tests](#10-tests)

---

## 1. The Problem

Pipeline failures in Fivetran occur in one of four layers:

| Layer | What it owns |
|---|---|
| **Connector** | Source authentication, sync scheduling, network reachability |
| **Schema** | Column presence, type compatibility, inclusion/exclusion config |
| **Transformation** | Downstream SQL/dbt model execution, dependency resolution |
| **Data Quality** | Row counts, null rates, duplicate rates, value range violations |

Each layer surfaces different signals, requires different API calls to investigate, and has a different remediation path. A failure in the connector layer looks like a schema failure downstream. A schema change breaks a transformation that raises a data quality alert. The layers are coupled; the failure origin is not obvious from the symptom.

Data engineers spend hours analysing incidents by manually cross-referencing the Fivetran dashboard, dbt run logs, and warehouse query outputs — before they can even begin to act. The signals exist. The APIs exist. The bottleneck is the triage loop.

PRE closes that loop: detect the failure via live sync, classify the root cause layer deterministically, dispatch specialist agents to investigate over the live Fivetran API, and route any fix through a human approval gate before executing on the real system.

---

## 2. Live Demo

**[https://pre-agent-498009659419.us-central1.run.app](https://pre-agent-498009659419.us-central1.run.app)**

When you click 'Sync' on a connection: PRE triggers a live sync, waits for it to settle, runs the linked transformation if applicable, and hands the resulting failure signal to the agent pipeline. Every run goes to the live Fivetran API.

- **Connection 1** — HITL flow: PRE detects the sync failure, diagnoses the root cause, queues a fix, and blocks until you approve or deny. Approving executes a real Fivetran API write and generates a Connect Card URL.
- **Connection 2** — Multi-agent handoff: sync succeeds, transformation fails. Schema agent investigates the type mismatch; findings are handed to the transformation agent.
- **Connection 3** — Schema diagnosis: transformation fails with a missing column. Schema agent traces the root cause to the sync configuration.

---

## 3. Architecture

```
  ── Monitoring Phase ──────────────────────────────────────────────────────
  │
  │  Fivetran sync triggered ──▶ poll until settled ──▶ run transformation
  │         │                                                    │
  │         └──── sync failed?                    transformation failed?
  │                    │                                         │
  │                    └─────────────┬───────────────────────────┘
  │                                  │
  │                        build signal payload
  │                      (sync_status / transformation_event)
  │
  ── Agent Phase ───────────────────────────────────────────────────────────
  │
  │                   ┌─────────────────────────────────────────┐
  │  signal ──▶       │           Orchestrator (PRE)            │
  │                   │  score_layers → classify → blast_radius │
  │                   └──────┬──────────────────────────────────┘
  │                          │  AgentTool dispatch (returns to orchestrator)
  │      ┌───────────────────┼──────────────────────┐
  │      ▼                   ▼                       ▼
  │ ┌──────────┐       ┌──────────┐        ┌─────────────────────┐
  │ │Connector │       │ Schema   │        │ Transformation /    │
  │ │  Agent   │       │  Agent   │        │ Data Quality Agent  │
  │ └────┬─────┘       └────┬─────┘        └──────────┬──────────┘
  │      └─────────────────┬┘──────────────────────────┘
  │                        │
  │             actions_queued_for_approval?
  │                        │
  │              ┌─────────▼──────────┐
  │              │    HITL Gate       │  ← async def awaits gate_queue.get()
  │              │  (truly blocking)  │    suspends runner.run_async() until
  │              └─────────┬──────────┘    human approves or denies
  │                        │ approved
  │              ┌─────────▼──────────┐
  │              │ Post-Fix Validation│  ← LoopAgent
  │              │                    │    (transformation_runner
  │              │                    │     → dq_validator × N)
  │              └─────────┬──────────┘
  │                        │
  │              ┌─────────▼──────────┐
  │              │   IncidentSummary  │  ← Pydantic-validated via
  │              │  resolved /        │    close_incident tool call
  │              │ escalated / partial│    (never from LLM text)
  │              └────────────────────┘
  │
  │  All agent tool calls ──▶ Fivetran MCP Server (server.py, 77 tools)
  │                                        │
  │                               Fivetran REST API
  ──────────────────────────────────────────────────────────────────────────
```

**Tech stack**

| Layer | Technology |
|---|---|
| Agent framework | Google ADK 2.1.0 (`LlmAgent`, `LoopAgent`, `AgentTool`, `Runner`) |
| Models | Gemini 3.1 Flash Lite on Vertex AI (`GOOGLE_CLOUD_LOCATION=global`) |
| Tool integration | Fivetran MCP Server (custom `server.py`, stdio transport) |
| HITL gate | Async Python function blocking `runner.run_async()` via `asyncio.Queue` |
| Demo server | FastAPI + Server-Sent Events + Alpine.js |
| Eval persistence | SQLite (local) / Firestore graceful fallback (Cloud Run) |
| Observability | OpenTelemetry → Google Cloud Trace (agent-level spans) |
| Deployment | Google Cloud Run (2Gi RAM, 2 CPU, 3600s timeout, min 1 instance) |

---

## 4. Multi-Agent Pipeline

Each failure layer gets its own specialist agent with a filtered tool set. A single agent with 77 tools available would face an unmanageable tool selection problem and could trigger a write without sufficient context.

| Agent | Tools exposed | Scope |
|---|---|---|
| Connector | `get_connection_details`, `run_connection_setup_tests`, `list_connections_in_group` | Auth, sync status, network reachability |
| Schema | `get_connection_schema_config`, `get_connection_column_config`, `list_transformations` | Column type drift, exclusions, schema diffs |
| Transformation | `get_transformation_details`, `run_transformation`, `get_connection_details` | Execution errors, dependency failures, re-run |
| Data Quality | `get_connection_details`, `get_connection_schema_config`, `get_transformation_details` | Null surges, duplicate rates, row count anomalies |

Write tools are inaccessible during diagnosis — only available to the orchestrator after HITL approval.

**4.1. Classification :** 
- `score_layers()` scores layers based on signal using a rule-based rubric. 
- `classify()` applies confidence thresholds. It assigns to earliest layer in tiebreakers. 
- Deterministic scoring. LLM dispatches.

**4.2. AgentTool :**  
- Sub-agents are registered as `AgentTool`, not `sub_agents`.
- With `AgentTool` - output returns as a `FunctionResponse` and the orchestrator continues.

**4.3. HITL gate :** 
- `request_approval` is an `async def` ADK tool that awaits `gate_queue.get()`, blocking `runner.run_async()` at the tool level until the human decides. 

**4.4. Post-fix validation :** 
- A `LoopAgent` confirms recovery. 
- Each validation agent must independently verify state via a read-tool call. 
- Fivetran write operations are asynchronous — a 200 confirms acceptance, not completion. 
- Recovery is only confirmed when a subsequent read returns a verified terminal state.

---

## 5. Fivetran MCP Integration

**5.1. Custom MCP server :**  
- The custom server exposes 77 tools covering connections, schemas, transformations, destinations, and groups. 
- It runs as a subprocess via MCP's stdio transport (`StdioServerParameters`).

**5.2. Two-toolset access model :**  
- Sub-agents: read-only `McpToolset` (`FIVETRAN_ALLOW_WRITES=false`) with a `tool_filter` scoping each agent to its domain. 
- Orchestrator: separate write-enabled instance, used only after HITL approval to execute the specific approved tool.

**5.3. Structured error responses :** 
- All Fivetran API errors (400–409) return `{"error": true, "code": N, "message": "..."}`. 
- Agents branch on error codes rather than treating every non-200 as fatal. 
- 409 Conflict is treated as confirmation (operation already in progress) — not retried.

**5.4. Response caching :** 
- GET responses are cached in-process with a 30s TTL. 
- Eliminates duplicate round-trips when multiple agents call the same read endpoint within a single run. 
- Write calls are never cached.

---

## 6. Engineering Challenges

### 6.1. LLM non-determinism

**A. Structured output** 
- Gemini wraps JSON in markdown fences, invents field names, and returns enum values outside the allowed set. 
- Every agent boundary has a normalization layer (`_strip_markdown_json`, `_coerce_agent_finding`, field aliasing) that absorbs these variations before Pydantic validation.

**B. Premature tool execution** 
- The orchestrator called the approved write tool in the same batch as `request_approval` before the gate could block it. 
- `LongRunningFunctionTool` pauses the outer loop, but the model's retries happen inside a single `runner.run_async()` call. 
- Making `request_approval` a native `async def` that suspends the runner eliminated the race.

**C. Unstructured closure** 
- The orchestrator occasionally produces reasoning prose instead of calling `close_incident`. 
- A synthesis fallback constructs a minimal escalation result from the last `AgentFinding` in session state so the eval DB always receives a usable row.

**D. False recovery confirmation** 
- Gemini treated write tool 200 responses as evidence of state change. 
- All write operations now require a subsequent read-tool call confirming terminal state before recovery is asserted.

**E. Context window growth** 
- Cascading incidents accumulate `agent_findings` that crowd out the system prompt. 
- A hard cap trims beyond a configurable threshold with a `logger.warning` on truncation.

### 6.2. MCP integration

**A. Subprocess environment** 
- Passing only Fivetran credentials to the subprocess env caused `ModuleNotFoundError`. 
- The subprocess must inherit the full `os.environ` to locate installed packages.

**B. `request_body` type mismatch** 
- The MCP server declared `request_body` as `"type": "string"`. 
- Agents queued gates with it as a Python dict. The first real HITL approval failed at MCP input validation. Fixed by accepting `oneOf: [string, object]`.

**C. Silent package absence** 
- ADK 2.1.0 wraps MCP imports in `try/except ImportError` and silently drops them if `mcp` is absent. 
- Agents initialised with zero tools and no error. `mcp>=1.0.0` added as an explicit dependency.

**D. ADK version pinning** 
- `google-adk==2.2.0` broke the `McpToolset` import path. 
- Pinned to `==2.1.0` in `pyproject.toml` and in the Dockerfile `pip install` step.

---

## 7. Production Hardening

PRE is designed to fail gracefully, not silently. Key mechanisms:

| Concern | Mechanism |
|---|---|
| Transient 429s | `HttpRetryOptions` |
| Sustained quota exhaustion | Outer retry loop |
| Run timeout | `asyncio.wait_for()` with escalation fallback |
| Tool hallucination | 3-strike force-break |
| Reasoning loops | Per-agent dispatch cap |
| Prompt injection | Input sanitization |
| Unstructured closure | Synthesis fallback |
| Write-before-verify | Pydantic-validated `IncidentSummary` is written by a tool call, not parsed from LLM text |
| Context overflow | Hard cap on `agent_findings` with `logger.warning` on truncation |


---

## 8. Eval Dashboard

Every run is persisted to the eval store (`/evals`). The dashboard tracks:

- **Two-phase latency**: monitoring phase vs agent phase  shown as separate columns. 
- **Per-agent breakdown**: duration and token attribution per agent run
- **OTPS** : output Tokens per second 
- **P95 / P50 baselines**: computed after 5 runs; `logger.warning` fires when a new run exceeds 2×P95 for duration or cost
- **Token budget alert**: fires from run 1 when `tokens_out > OUTPUT_TOKEN_BUDGET`

---

## 9. Running Locally

**Prerequisites**: Python 3.11+, a Fivetran account with API credentials, a GCP project with Vertex AI enabled.

```bash
git clone https://github.com/BishopSlice/PipelineReliability.git
cd PipelineReliability

# Install dependencies
pip install -e .

# Configure credentials
cp .env.example .env
# Edit .env: set FIVETRAN_API_KEY, FIVETRAN_API_SECRET, GOOGLE_CLOUD_PROJECT,
#            GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_LOCATION=global

# Authenticate with Google Cloud
gcloud auth application-default login

# Start the demo server
python3 -m uvicorn demo.server:app --host 0.0.0.0 --port 8000

# Open http://localhost:8000
```

**Cloud Run deployment** (requires `gcloud` CLI authenticated):
```bash
bash scripts/deploy.sh
```

---

## 10. Tests

97 deterministic unit tests cover the guardrail layer — the code that wraps and validates LLM outputs before they reach session state. This is where silent regressions are most dangerous: a coercion bug doesn't crash the system, it mis-routes an incident.

```bash
python3 -m pytest tests/ -v   # no credentials required
```

Coverage: signal model validation, classifier scoring (all confounders and tiebreak rules), context coercion, input sanitization, blast radius normalization.

Full agent runs require live Fivetran credentials and are validated manually against the three demo connections before each release. LLM non-determinism makes end-to-end runs unsuitable for automated CI without response replay.

---

## License

MIT — see [LICENSE](LICENSE).
