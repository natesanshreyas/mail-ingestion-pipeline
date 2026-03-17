#!/bin/bash
# register_mailbox.sh — Onboard a mailbox into the ingestion pipeline.
#
# Adds the mailbox to Cosmos DB and immediately creates a Graph change-
# notification subscription pointing at the ASB queue.
# (Idempotent — safe to run again; existing active subscription is kept.)
#
# Usage:
#   ./scripts/register_mailbox.sh <mailbox_id> [display_name]
#
# Requires env vars (or reads from terraform output if run from repo root):
#   GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_CLIENT_STATE
#   ASB_SEND_CONNECTION_STRING, ASB_QUEUE_NAME
#   COSMOS_ENDPOINT, COSMOS_KEY

set -euo pipefail

MAILBOX_ID="${1:-}"
DISPLAY_NAME="${2:-}"

if [[ -z "$MAILBOX_ID" ]]; then
  echo "Usage: $0 <mailbox_id> [display_name]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Load from terraform output if vars not already set
if [[ -z "${ASB_SEND_CONNECTION_STRING:-}" ]]; then
  echo "Reading values from terraform output..."
  cd "$REPO_ROOT/infra"
  export ASB_SEND_CONNECTION_STRING=$(terraform output -raw asb_send_connection_string)
  export ASB_QUEUE_NAME=$(terraform output -raw asb_queue_name)
  export COSMOS_ENDPOINT=$(terraform output -raw cosmos_endpoint)
  export COSMOS_KEY=$(terraform output -raw cosmos_primary_key)
  cd "$REPO_ROOT"
fi

for var in GRAPH_TENANT_ID GRAPH_CLIENT_ID GRAPH_CLIENT_SECRET GRAPH_CLIENT_STATE \
           ASB_SEND_CONNECTION_STRING ASB_QUEUE_NAME COSMOS_ENDPOINT COSMOS_KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: env var $var is required but not set."
    exit 1
  fi
done

echo "Onboarding mailbox: $MAILBOX_ID"

# Run the Python registration script inside a temporary venv if needed
python3 - <<PYEOF
import sys, os
sys.path.insert(0, "$REPO_ROOT/src/shared")
sys.path.insert(0, "$REPO_ROOT/src/reconciler")

from graph_client import GraphClient, asb_notification_url
from cosmos_client import MailboxStore

mailbox_id   = "$MAILBOX_ID"
display_name = "$DISPLAY_NAME"

graph = GraphClient(
    os.environ["GRAPH_TENANT_ID"],
    os.environ["GRAPH_CLIENT_ID"],
    os.environ["GRAPH_CLIENT_SECRET"],
)
store = MailboxStore(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"])

# Upsert mailbox in Cosmos DB
doc = store.upsert_mailbox(mailbox_id, display_name)

# Check for existing active subscription
active = [s for s in doc.get("subscriptions", []) if s.get("status") == "active"]
if active:
    print(f"Mailbox already has {len(active)} active subscription(s) — nothing to do.")
    sys.exit(0)

# Build the ServiceBus: notification URL and create subscription
notification_url = asb_notification_url(
    os.environ["ASB_SEND_CONNECTION_STRING"],
    os.environ["ASB_QUEUE_NAME"],
)
sub = graph.create_subscription(
    mailbox_id=mailbox_id,
    notification_url=notification_url,
    client_state=os.environ["GRAPH_CLIENT_STATE"],
)
store.add_subscription(mailbox_id, sub)

print(f"Subscription created: {sub['id']}")
print(f"Expires: {sub['expirationDateTime']}")
print(f"Mailbox {mailbox_id} is now active in the pipeline.")
PYEOF
