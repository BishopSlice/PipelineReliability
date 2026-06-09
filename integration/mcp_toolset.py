from __future__ import annotations

import json
import os
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters


def get_vertex_extra_headers() -> dict[str, str] | None:
    """D-48: Return extra HTTP headers for Vertex AI inference requests.

    Headers are read from the VERTEX_AI_EXTRA_HEADERS env var (a JSON object).
    Returns None when the var is unset or empty, leaving HttpOptions unchanged.

    Example:
        VERTEX_AI_EXTRA_HEADERS={"X-Goog-User-Project": "my-project-id"}
    """
    raw = os.getenv("VERTEX_AI_EXTRA_HEADERS", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) and parsed else None
    except (json.JSONDecodeError, TypeError):
        return None


def _base_env(allow_writes: bool) -> dict[str, str]:
    """Build env dict for the Fivetran MCP subprocess.
    Inherits the full current environment so server.py can find installed packages,
    then overrides the Fivetran-specific vars.
    """
    api_key = os.environ.get("FIVETRAN_API_KEY")
    api_secret = os.environ.get("FIVETRAN_API_SECRET")

    if not api_key or not api_secret:
        raise EnvironmentError(
            "FIVETRAN_API_KEY and FIVETRAN_API_SECRET must be set in environment. "
            "Copy .env.example to .env and fill in your credentials."
        )

    return {
        **os.environ,                   # inherit PATH, PYTHONPATH, etc. so server.py can import deps
        "FIVETRAN_API_KEY": api_key,
        "FIVETRAN_API_SECRET": api_secret,
        "FIVETRAN_ALLOW_WRITES": "true" if allow_writes else "false",
    }


def get_sub_agent_toolset(tool_filter: list[str]) -> McpToolset:
    """
    Read-only MCPToolset for sub-agents.
    FIVETRAN_ALLOW_WRITES=false — write calls are rejected at server level.
    tool_filter scopes the agent to exactly its authorised discovery set.
    """
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="python3",
                args=["server.py"],
                env=_base_env(allow_writes=False),
            )
        ),
        tool_filter=tool_filter,
    )


def get_orchestrator_toolset() -> McpToolset:
    """
    Write-enabled MCPToolset for the orchestrator.
    Used only after HITL approval — orchestrator executes approved write calls.
    No tool_filter: orchestrator may need any tool for blast radius assessment
    and for executing approved mutations.
    """
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="python3",
                args=["server.py"],
                env=_base_env(allow_writes=True),
            )
        ),
    )
