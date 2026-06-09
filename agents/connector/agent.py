from __future__ import annotations

import os
from google.adk.agents import LlmAgent
from google.genai import types as genai_types

from agents.connector.prompts import CONNECTOR_INSTRUCTIONS
from shared.context import after_agent_callback
from integration.mcp_toolset import get_sub_agent_toolset, get_vertex_extra_headers

CONNECTOR_TOOL_FILTER = [
    "get_connection_details",
    "get_connection_state",
    "list_connections",
    "list_connections_in_group",
    "get_connection_schema_config",
    "run_connection_setup_tests",
    "list_metadata_connectors",
    "get_destination_details",
]

connector_agent = LlmAgent(
    name="connector_agent",
    model=os.getenv("SUB_AGENT_MODEL", "gemini-3.1-flash-lite"),
    description=(
        "Diagnoses connector layer failures: sync errors, auth issues, rate limiting, "
        "network failures, and paused connections. Dispatched when sync_status signals "
        "implicate the source-to-Fivetran layer."
    ),
    instruction=CONNECTOR_INSTRUCTIONS,
    tools=[get_sub_agent_toolset(CONNECTOR_TOOL_FILTER)],
    disallow_transfer_to_peers=True,
    after_agent_callback=after_agent_callback,
    output_key="connector_agent_output",
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)
