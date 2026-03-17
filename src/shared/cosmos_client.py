"""
cosmos_client.py — Cosmos DB client for mailbox and subscription tracking.

Container: mail-ingestion / mailboxes
Partition key: /mailboxId

Document shape:
{
  "id":           "user@domain.com",          # same as mailboxId
  "mailboxId":    "user@domain.com",          # partition key
  "displayName":  "Human Name",
  "status":       "active",                   # active | suspended
  "createdAt":    "<ISO-8601>",
  "updatedAt":    "<ISO-8601>",
  "subscriptions": [
    {
      "subscriptionId": "...",
      "resource":       "users/.../messages",
      "changeTypes":    "created,updated",
      "notificationUrl": "...",
      "expiresAt":      "<ISO-8601>",
      "status":         "active"              # active | renewing | expired
    }
  ]
}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from azure.cosmos import CosmosClient, PartitionKey, exceptions

logger = logging.getLogger(__name__)

DATABASE_NAME = "mail-ingestion"
CONTAINER_NAME = "mailboxes"


class MailboxStore:
    def __init__(self, endpoint: str, key: str) -> None:
        client = CosmosClient(endpoint, key)
        db = client.create_database_if_not_exists(DATABASE_NAME)
        self._container = db.create_container_if_not_exists(
            id=CONTAINER_NAME,
            partition_key=PartitionKey(path="/mailboxId"),
            offer_throughput=400,
        )

    # ------------------------------------------------------------------
    # Mailbox CRUD
    # ------------------------------------------------------------------

    def upsert_mailbox(self, mailbox_id: str, display_name: str = "") -> dict:
        """Create or return a mailbox document."""
        now = _now()
        try:
            doc = self._container.read_item(item=mailbox_id, partition_key=mailbox_id)
            doc["updatedAt"] = now
            doc["displayName"] = display_name or doc.get("displayName", "")
        except exceptions.CosmosResourceNotFoundError:
            doc = {
                "id": mailbox_id,
                "mailboxId": mailbox_id,
                "displayName": display_name,
                "status": "active",
                "createdAt": now,
                "updatedAt": now,
                "subscriptions": [],
            }
        return self._container.upsert_item(doc)

    def get_mailbox(self, mailbox_id: str) -> dict | None:
        try:
            return self._container.read_item(item=mailbox_id, partition_key=mailbox_id)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def list_mailboxes(self, status: str = "active") -> list[dict]:
        query = "SELECT * FROM c WHERE c.status = @status"
        params = [{"name": "@status", "value": status}]
        return list(
            self._container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True,
            )
        )

    def delete_mailbox(self, mailbox_id: str) -> None:
        self._container.delete_item(item=mailbox_id, partition_key=mailbox_id)

    # ------------------------------------------------------------------
    # Subscription management within a mailbox document
    # ------------------------------------------------------------------

    def add_subscription(self, mailbox_id: str, subscription: dict) -> dict:
        """Append (or replace) a subscription record in the mailbox document."""
        doc = self.upsert_mailbox(mailbox_id)
        subs: list[dict] = doc.get("subscriptions", [])

        # Replace existing entry with same subscriptionId if present
        subs = [s for s in subs if s.get("subscriptionId") != subscription["subscriptionId"]]
        subs.append({
            "subscriptionId": subscription["subscriptionId"],
            "resource":        subscription.get("resource", ""),
            "changeTypes":     subscription.get("changeType", "created,updated"),
            "notificationUrl": subscription.get("notificationUrl", ""),
            "expiresAt":       subscription.get("expirationDateTime", ""),
            "status":          "active",
        })
        doc["subscriptions"] = subs
        doc["updatedAt"] = _now()
        return self._container.upsert_item(doc)

    def update_subscription_expiry(
        self, mailbox_id: str, subscription_id: str, new_expiry: str
    ) -> None:
        doc = self.get_mailbox(mailbox_id)
        if not doc:
            return
        for sub in doc.get("subscriptions", []):
            if sub["subscriptionId"] == subscription_id:
                sub["expiresAt"] = new_expiry
                sub["status"] = "active"
                break
        doc["updatedAt"] = _now()
        self._container.upsert_item(doc)

    def mark_subscription_expired(self, mailbox_id: str, subscription_id: str) -> None:
        doc = self.get_mailbox(mailbox_id)
        if not doc:
            return
        for sub in doc.get("subscriptions", []):
            if sub["subscriptionId"] == subscription_id:
                sub["status"] = "expired"
                break
        doc["updatedAt"] = _now()
        self._container.upsert_item(doc)

    def remove_subscription(self, mailbox_id: str, subscription_id: str) -> None:
        doc = self.get_mailbox(mailbox_id)
        if not doc:
            return
        doc["subscriptions"] = [
            s for s in doc.get("subscriptions", [])
            if s.get("subscriptionId") != subscription_id
        ]
        doc["updatedAt"] = _now()
        self._container.upsert_item(doc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
