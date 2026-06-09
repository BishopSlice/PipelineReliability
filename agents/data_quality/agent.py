from __future__ import annotations

import os
from google.adk.agents import LlmAgent
from google.genai import types as genai_types

from agents.data_quality.prompts import DATA_QUALITY_INSTRUCTIONS
from shared.context import after_agent_callback_with_loop_exit
from integration.mcp_toolset import get_sub_agent_toolset, get_vertex_extra_headers

DATA_QUALITY_TOOL_FILTER = [
    "get_connection_details",       # use succeeded_at for sync state — get_connection_state returns 405 on many connector types
    "get_connection_schema_config",
    "get_connection_column_config",
    "list_connections",
    "list_connections_in_group",
    "get_transformation_details",
]

data_quality_agent = LlmAgent(
    name="data_quality_agent",
    model=os.getenv("SUB_AGENT_MODEL", "gemini-3.1-flash-lite"),
    description=(
        "Diagnoses data quality anomalies (null surges, duplicates, row count deviations, "
        "value range violations) and serves as the validation judge at the end of incident "
        "recovery chains. Signals LoopAgent exit when recovery is confirmed."
    ),
    instruction=DATA_QUALITY_INSTRUCTIONS,
    tools=[get_sub_agent_toolset(DATA_QUALITY_TOOL_FILTER)],
    disallow_transfer_to_peers=True,
    after_agent_callback=after_agent_callback_with_loop_exit,
    output_key="data_quality_agent_output",
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)
