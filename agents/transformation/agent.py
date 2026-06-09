from __future__ import annotations

import os
from google.adk.agents import LlmAgent
from google.genai import types as genai_types

from agents.transformation.prompts import TRANSFORMATION_INSTRUCTIONS
from shared.context import after_agent_callback
from integration.mcp_toolset import get_sub_agent_toolset, get_vertex_extra_headers

TRANSFORMATION_TOOL_FILTER = [
    "get_transformation_details",
    "list_transformations",
    "list_transformation_projects",
    "get_transformation_project_details",
    "get_transformation_package_metadata_details",
    "list_transformation_package_metadata",
    "get_connection_details",   # use succeeded_at/failed_at for sync state — get_connection_state returns 405 on some connector types
    "run_transformation",       # needed in post_fix_validation to re-trigger after a schema fix
]

transformation_agent = LlmAgent(
    name="transformation_agent",
    model=os.getenv("SUB_AGENT_MODEL", "gemini-3.1-flash-lite"),
    description=(
        "Diagnoses transformation layer failures: model errors, stale data execution, "
        "dependency violations. Also coordinates the post-fix sync → re-run sequence "
        "after upstream schema fixes are applied."
    ),
    instruction=TRANSFORMATION_INSTRUCTIONS,
    tools=[get_sub_agent_toolset(TRANSFORMATION_TOOL_FILTER)],
    disallow_transfer_to_peers=True,
    after_agent_callback=after_agent_callback,
    output_key="transformation_agent_output",
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(initial_delay=2, attempts=3),
            headers=get_vertex_extra_headers(),
        ),
    ),
)
