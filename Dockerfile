FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed by some Google Cloud packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . .

# Pin ADK before installing everything else to prevent pip from resolving 2.2.0+
RUN pip install --no-cache-dir "google-adk==2.1.0"

# Install remaining dependencies
RUN pip install --no-cache-dir -e .

EXPOSE 8080

# MCP server (server.py) is spawned as a stdio subprocess by the ADK agent
# on each tool call — no separate process needed here.
# Cloud Run injects PORT env var; default 8080 matches EXPOSE above.
CMD exec python3 -m uvicorn demo.server:app --host 0.0.0.0 --port "${PORT:-8080}"
