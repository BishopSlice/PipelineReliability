from __future__ import annotations

import os
from google.adk.agents import LlmAgent
from google.genai import types as genai_types

from agents.schema.prompts import SCHEMA_INSTRUCTIONS
from shared.context import after_agent_callback
from integration.mcp_toolset import get_sub_agent_toolset, get_vertex_extra_headers

SCHEMA_TOOL_FILTER = [
    "get_connection_schema_config",
    "get_connection_column_config",
    "get_connection_details",
    "list_connections",
    "list_connections_in_group",
    "list_transformations",
    "list_transformation_projects",
    "get_metadata_connector_config",
]

schema_agent = LlmAgent(
    name="schema_agent",
    model=os.getenv("SUB_AGENT_MODEL", "gemini-3.1-flash-lite"),
    description=(
        "Diagnoses schema layer failures: column type drift, column exclusions, missing columns, "
        "and schema configuration anomalies. Dispatched when schema_diff signals are present or "
        "when transformation errors implicate missing/changed schema objects."
    ),
    instruction=SCHEMA_INSTRUCTIONS,
    tools=[get_sub_agent_toolset(SCHEMA_TOOL_FILTER)],
    disallow_transfer_to_peers=True,
    after_agent_callback=after_agent_callback,
    output_key="schema_agent_output",
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)
