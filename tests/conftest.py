"""Add project root to sys.path so all tests can import project modules."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Stub credentials so ADK/MCP toolset modules load without an .env file.
# These are never used to make real Fivetran calls in tests.
os.environ.setdefault("FIVETRAN_API_KEY", "test-key-stub")
os.environ.setdefault("FIVETRAN_API_SECRET", "test-secret-stub")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "0")
