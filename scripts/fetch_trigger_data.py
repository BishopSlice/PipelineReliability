"""
Fetches real Fivetran API data for the three demo connectors and prints
everything needed to update the three trigger JSON files.

Usage:
    python3 scripts/fetch_trigger_data.py \
        --connector-failure <id> \
        --column-excluded <id> \
        --schema-cascade <id>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

# Load .env from project root
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

try:
    import httpx
except ImportError:
    sys.exit("httpx not installed — run: pip install httpx")


def _client() -> httpx.Client:
    key    = os.environ["FIVETRAN_API_KEY"]
    secret = os.environ["FIVETRAN_API_SECRET"]
    token  = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return httpx.Client(
        base_url="https://api.fivetran.com/v1",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        timeout=15,
    )


def get(client: httpx.Client, path: str) -> dict:
    r = client.get(path)
    r.raise_for_status()
    return r.json().get("data", r.json())


def fetch_connector(client: httpx.Client, cid: str, label: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}  (id: {cid})")
    print(f"{'='*60}")

    details = get(client, f"/connections/{cid}")
    print("\n--- get_connection_details ---")
    print(json.dumps({
        "id":           details.get("id"),
        "schema":       details.get("schema"),
        "service":      details.get("service"),
        "status":       details.get("status", {}).get("sync_state"),
        "setup_state":  details.get("status", {}).get("setup_state"),
        "error_code":   details.get("status", {}).get("setup_state"),
        "failures":     details.get("failed_at"),
    }, indent=2))

    # Full status block for the trigger file
    print("\n--- Full status block (for trigger file) ---")
    print(json.dumps(details.get("status", {}), indent=2))

    try:
        schema = get(client, f"/connections/{cid}/schemas")
        print("\n--- get_connection_schema_config (first schema/table) ---")
        schemas = schema.get("schemas", {})
        for schema_name, schema_body in schemas.items():
            tables = schema_body.get("tables", {})
            for table_name, table_body in tables.items():
                columns = table_body.get("columns", {})
                print(f"  schema: {schema_name}  table: {table_name}")
                for col_name, col_body in columns.items():
                    if not col_body.get("enabled", True):
                        print(f"    EXCLUDED column: {col_name}")
                    else:
                        print(f"    column: {col_name}  enabled={col_body.get('enabled')}  type={col_body.get('data_type','?')}")
    except Exception as e:
        print(f"  (schema config not available: {e})")


def fetch_transformations(client: httpx.Client) -> None:
    print(f"\n{'='*60}")
    print("  TRANSFORMATIONS")
    print(f"{'='*60}")
    try:
        result = get(client, "/transformations")
        items = result if isinstance(result, list) else result.get("items", [])
        for t in items:
            print(json.dumps({
                "id":     t.get("id"),
                "name":   t.get("name") or t.get("title"),
                "status": t.get("status"),
                "connector_ids": t.get("connector_ids", []),
            }, indent=2))
    except Exception as e:
        print(f"  (transformations endpoint error: {e})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connector-failure", required=True)
    parser.add_argument("--column-excluded",   required=True)
    parser.add_argument("--schema-cascade",    required=True)
    args = parser.parse_args()

    with _client() as c:
        fetch_connector(c, args.connector_failure, "Scenario 1 — Connector Failure")
        fetch_connector(c, args.column_excluded,   "Scenario 2 — Column Excluded")
        fetch_connector(c, args.schema_cascade,    "Scenario 3 — Schema Cascade")
        fetch_transformations(c)


if __name__ == "__main__":
    main()
