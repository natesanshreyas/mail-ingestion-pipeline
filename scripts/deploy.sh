#!/bin/bash
# =============================================================================
# deploy.sh — One-shot bootstrap for the mail-ingestion-pipeline.
#
# Assumes:
#   1. Terraform has been applied (infra/) and outputs are available.
#   2. az, kubectl, docker, terraform are installed and you are logged in.
#
# Required env vars:
#   GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
#   GRAPH_CLIENT_STATE
#   OPENAI_API_KEY
#
# Everything else is read from `terraform output`.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
INFRA_DIR="$REPO_ROOT/infra"
K8S_DIR="$REPO_ROOT/k8s"

# ---------------------------------------------------------------------------
# Validate required env vars
# ---------------------------------------------------------------------------
for var in GRAPH_TENANT_ID GRAPH_CLIENT_ID GRAPH_CLIENT_SECRET GRAPH_CLIENT_STATE OPENAI_API_KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: env var $var is required but not set."
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Read Terraform outputs
# ---------------------------------------------------------------------------
echo "[1/6] Reading Terraform outputs..."
cd "$INFRA_DIR"

ACR_LOGIN_SERVER=$(terraform output -raw acr_login_server)
AKS_CLUSTER=$(terraform output -raw aks_cluster_name)
RG=$(terraform output -raw resource_group_name)
ASB_SEND_CS=$(terraform output -raw asb_send_connection_string)
ASB_LISTEN_CS=$(terraform output -raw asb_listen_connection_string)
ASB_QUEUE=$(terraform output -raw asb_queue_name)
EH_CS=$(terraform output -raw eventhub_send_connection_string)
STORAGE_CS=$(terraform output -raw storage_connection_string)
COSMOS_ENDPOINT=$(terraform output -raw cosmos_endpoint)
COSMOS_KEY=$(terraform output -raw cosmos_primary_key)

echo "  ACR  : $ACR_LOGIN_SERVER"
echo "  AKS  : $AKS_CLUSTER ($RG)"
echo "  ASB  : queue=$ASB_QUEUE"

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Build & push Docker images
# ---------------------------------------------------------------------------
echo "[2/6] Building and pushing Docker images..."
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

az acr login --name "$(echo "$ACR_LOGIN_SERVER" | cut -d. -f1)"

for svc in reconciler ingestion_worker; do
  name=$(echo "$svc" | tr '_' '-')
  docker build \
    -f "src/$svc/Dockerfile" \
    -t "$ACR_LOGIN_SERVER/$name:$IMAGE_TAG" \
    -t "$ACR_LOGIN_SERVER/$name:latest" \
    src
  docker push "$ACR_LOGIN_SERVER/$name:$IMAGE_TAG"
  docker push "$ACR_LOGIN_SERVER/$name:latest"
  echo "  Pushed: $name"
done

# ---------------------------------------------------------------------------
# Configure kubectl
# ---------------------------------------------------------------------------
echo "[3/6] Configuring kubectl..."
az aks get-credentials --resource-group "$RG" --name "$AKS_CLUSTER" --overwrite-existing

# ---------------------------------------------------------------------------
# Apply base manifests
# ---------------------------------------------------------------------------
echo "[4/6] Applying Kubernetes base manifests..."
kubectl apply -f "$K8S_DIR/namespace.yaml"
kubectl apply -f "$K8S_DIR/configmap.yaml"

# ---------------------------------------------------------------------------
# Create the Kubernetes secret
# ---------------------------------------------------------------------------
echo "[5/6] Creating Kubernetes secret..."
kubectl create secret generic mail-ingestion-secrets \
  --namespace mail-ingestion \
  --from-literal=GRAPH_TENANT_ID="$GRAPH_TENANT_ID" \
  --from-literal=GRAPH_CLIENT_ID="$GRAPH_CLIENT_ID" \
  --from-literal=GRAPH_CLIENT_SECRET="$GRAPH_CLIENT_SECRET" \
  --from-literal=GRAPH_CLIENT_STATE="$GRAPH_CLIENT_STATE" \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
  --from-literal=ASB_CONNECTION_STRING="$ASB_LISTEN_CS" \
  --from-literal=ASB_SEND_CONNECTION_STRING="$ASB_SEND_CS" \
  --from-literal=ASB_QUEUE_NAME="$ASB_QUEUE" \
  --from-literal=EVENTHUB_CONNECTION_STRING="$EH_CS" \
  --from-literal=STORAGE_CONNECTION_STRING="$STORAGE_CS" \
  --from-literal=COSMOS_ENDPOINT="$COSMOS_ENDPOINT" \
  --from-literal=COSMOS_KEY="$COSMOS_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# Deploy workloads
# ---------------------------------------------------------------------------
echo "[6/6] Deploying workloads..."

for manifest in \
  "$K8S_DIR/reconciler/cronjob.yaml" \
  "$K8S_DIR/ingestion-worker/deployment.yaml"
do
  sed \
    -e "s|\${ACR_LOGIN_SERVER}|$ACR_LOGIN_SERVER|g" \
    -e "s|\${IMAGE_TAG}|$IMAGE_TAG|g" \
    "$manifest" | kubectl apply -f -
done

kubectl apply -f "$K8S_DIR/ingestion-worker/service.yaml"
kubectl apply -f "$K8S_DIR/ingestion-worker/hpa.yaml"

kubectl rollout status deployment/ingestion-worker -n mail-ingestion --timeout=120s

echo ""
echo "================================================================="
echo " Deployment complete!"
echo "================================================================="
echo " ASB queue    : $ASB_QUEUE (Graph pushes here directly)"
echo " Event Hubs   : email-intents (classified JSON output)"
echo " AKS cluster  : $AKS_CLUSTER"
echo "================================================================="
echo ""
echo " Onboard a mailbox:"
echo "   ./scripts/register_mailbox.sh user@domain.com \"Display Name\""
echo ""
echo " Monitor worker logs:"
echo "   kubectl logs -n mail-ingestion -l app=ingestion-worker -f"
echo ""
echo " Trigger reconciler immediately:"
echo "   kubectl create job --from=cronjob/subscription-reconciler reconciler-manual -n mail-ingestion"
