#!/usr/bin/env bash
# Deploy PRE Agent to Cloud Run (D-53).
# Usage: bash scripts/deploy.sh
# Requires: gcloud CLI authenticated, .env present in project root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

# Load .env
if [ ! -f "$ROOT/.env" ]; then
  echo "ERROR: .env not found at $ROOT/.env"
  echo "Copy .env.example to .env and fill in credentials."
  exit 1
fi
set -a
# shellcheck source=/dev/null
source "$ROOT/.env"
set +a

# Required vars
: "${FIVETRAN_API_KEY:?FIVETRAN_API_KEY must be set in .env}"
: "${FIVETRAN_API_SECRET:?FIVETRAN_API_SECRET must be set in .env}"

PROJECT="fivetran-111"
REGION="us-central1"
SERVICE="pre-agent"

echo "==> Deploying $SERVICE to Cloud Run ($REGION)..."

gcloud run deploy "$SERVICE" \
  --source "$ROOT" \
  --project "$PROJECT" \
  --region "$REGION" \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 3 \
  --port 8080 \
  --allow-unauthenticated \
  --set-env-vars "\
GOOGLE_GENAI_USE_VERTEXAI=true,\
GOOGLE_CLOUD_PROJECT=fivetran-111,\
GOOGLE_CLOUD_LOCATION=global,\
FIVETRAN_API_KEY=${FIVETRAN_API_KEY},\
FIVETRAN_API_SECRET=${FIVETRAN_API_SECRET}"

echo ""
echo "==> Done. Visit the URL above to test the deployment."
echo "    Smoke test: curl <URL>/health"
