"""
reconciler.py — Subscription reconciliation job.

Runs as a Kubernetes CronJob (every hour). Compares mailboxes tracked in
Cosmos DB against active Graph subscriptions, then:

  1. Creates subscriptions (pointing at the ASB queue via ServiceBus: URI)
     for any mailbox that has none.
  2. Renews subscriptions expiring within RENEWAL_THRESHOLD_HOURS.
  3. Marks subscriptions as expired if they no longer appear in Graph.

No public endpoint required — Graph pushes notifications directly to ASB.

Exit codes:
  0 — success
  1 — fatal error
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/shared")
from graph_client import GraphClient, RENEWAL_THRESHOLD_HOURS, asb_notification_url
from cosmos_client import MailboxStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("reconciler")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GRAPH_TENANT_ID    = os.environ["GRAPH_TENANT_ID"]
GRAPH_CLIENT_ID    = os.environ["GRAPH_CLIENT_ID"]
GRAPH_CLIENT_SECRET = os.environ["GRAPH_CLIENT_SECRET"]
GRAPH_CLIENT_STATE  = os.environ.get("GRAPH_CLIENT_STATE", "mail-ingestion-secret")

COSMOS_ENDPOINT = os.environ["COSMOS_ENDPOINT"]
COSMOS_KEY      = os.environ["COSMOS_KEY"]

# ASB connection string with Send permission on the notifications queue
ASB_CONNECTION_STRING = os.environ["ASB_CONNECTION_STRING"]
ASB_QUEUE_NAME        = os.environ.get("ASB_QUEUE_NAME", "mail-notifications")


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------

class Reconciler:
    def __init__(self) -> None:
        self.graph = GraphClient(GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET)
        self.store = MailboxStore(COSMOS_ENDPOINT, COSMOS_KEY)
        # Build the ServiceBus: notification URL once — valid for 7 days,
        # well beyond the 3-day Graph subscription TTL.
        self.notification_url = asb_notification_url(ASB_CONNECTION_STRING, ASB_QUEUE_NAME)

    def run(self) -> dict:
        stats = {
            "mailboxes_checked":      0,
            "subscriptions_created":  0,
            "subscriptions_renewed":  0,
            "subscriptions_expired":  0,
            "errors":                 0,
        }

        logger.info("=" * 60)
        logger.info("Reconciliation started: %s", _now())
        logger.info("=" * 60)

        mailboxes = self.store.list_mailboxes(status="active")
        logger.info("Tracked mailboxes: %d", len(mailboxes))

        # Index live Graph subscriptions by mailboxId
        live_subs: dict[str, dict] = {s["id"]: s for s in self.graph.list_subscriptions()}
        logger.info("Live Graph subscriptions: %d", len(live_subs))

        mailbox_to_live: dict[str, list[str]] = {}
        for sub_id, sub in live_subs.items():
            mbx = _mailbox_from_resource(sub.get("resource", ""))
            if mbx:
                mailbox_to_live.setdefault(mbx, []).append(sub_id)

        for doc in mailboxes:
            mailbox_id: str = doc["mailboxId"]
            stats["mailboxes_checked"] += 1

            stored_active = {
                s["subscriptionId"]
                for s in doc.get("subscriptions", [])
                if s.get("status") == "active"
            }
            live_for_mailbox = set(mailbox_to_live.get(mailbox_id, []))

            # Mark orphaned stored subs as expired
            for sub_id in stored_active - live_for_mailbox:
                logger.info("[%s] Subscription %s gone from Graph → expired", mailbox_id, sub_id)
                self.store.mark_subscription_expired(mailbox_id, sub_id)
                stats["subscriptions_expired"] += 1

            # Renew subs expiring soon
            for sub_id in live_for_mailbox:
                if _expiring_soon(live_subs[sub_id].get("expirationDateTime", "")):
                    try:
                        renewed = self.graph.renew_subscription(sub_id)
                        self.store.update_subscription_expiry(
                            mailbox_id, sub_id, renewed["expirationDateTime"]
                        )
                        stats["subscriptions_renewed"] += 1
                    except Exception as exc:
                        logger.error("[%s] Renewal of %s failed: %s", mailbox_id, sub_id, exc)
                        stats["errors"] += 1

            # Create subscription if none exist
            if not live_for_mailbox:
                try:
                    sub = self.graph.create_subscription(
                        mailbox_id=mailbox_id,
                        notification_url=self.notification_url,
                        client_state=GRAPH_CLIENT_STATE,
                    )
                    self.store.add_subscription(mailbox_id, sub)
                    stats["subscriptions_created"] += 1
                    logger.info("[%s] Created subscription %s", mailbox_id, sub["id"])
                except Exception as exc:
                    logger.error("[%s] Subscription creation failed: %s", mailbox_id, exc)
                    stats["errors"] += 1

        logger.info("=" * 60)
        logger.info("Reconciliation complete: %s", stats)
        logger.info("=" * 60)
        return stats


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiring_soon(expiry_str: str) -> bool:
    if not expiry_str:
        return True
    try:
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        return expiry <= datetime.now(timezone.utc) + timedelta(hours=RENEWAL_THRESHOLD_HOURS)
    except ValueError:
        return True


def _mailbox_from_resource(resource: str) -> str:
    parts = [p for p in resource.replace("\\", "/").split("/") if p]
    for i, part in enumerate(parts):
        if part.lower() in ("users", "user") and i + 1 < len(parts):
            return parts[i + 1]
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        stats = Reconciler().run()
        sys.exit(0 if stats["errors"] == 0 else 1)
    except Exception as exc:
        logger.exception("Reconciler crashed: %s", exc)
        sys.exit(1)
