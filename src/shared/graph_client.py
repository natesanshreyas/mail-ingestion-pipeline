"""
graph_client.py — Shared Microsoft Graph API client.

Handles:
  - Service-principal token acquisition (MSAL)
  - Subscription CRUD (create, renew, delete, list)
  - Delta query (fetch new/changed messages per mailbox)

When using Azure Service Bus as the notification endpoint, Graph subscriptions
use the ServiceBus: URI scheme — no public HTTPS webhook required:

  notificationUrl = "ServiceBus:https://<ns>.servicebus.windows.net/<queue>?<SAS>"

The helper `asb_notification_url()` builds this from a standard ASB
connection string.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import urllib.parse
from base64 import b64encode, b64decode
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import msal

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# Graph mail subscriptions expire after at most 4230 minutes (~3 days).
# The reconciler renews anything expiring within 48 h.
SUBSCRIPTION_TTL_MINUTES = 4230
RENEWAL_THRESHOLD_HOURS = 48


# ---------------------------------------------------------------------------
# ASB notification URL helper
# ---------------------------------------------------------------------------

def asb_notification_url(connection_string: str, queue_name: str, ttl_seconds: int = 86400 * 7) -> str:
    """
    Build the ServiceBus: notification URL that Graph expects when you want
    it to push change notifications directly to an ASB queue.

    Graph documentation:
      https://learn.microsoft.com/graph/change-notifications-delivery-service-bus

    The URL format is:
      ServiceBus:https://<namespace>.servicebus.windows.net/<queue>?<SAS-params>
    """
    # Parse the connection string
    parts = dict(
        segment.split("=", 1)
        for segment in connection_string.split(";")
        if "=" in segment
    )
    endpoint: str = parts.get("Endpoint", "")          # sb://namespace.servicebus.windows.net/
    key_name: str = parts.get("SharedAccessKeyName", "")
    key_value: str = parts.get("SharedAccessKey", "")

    # Derive the HTTPS resource URI
    namespace = endpoint.replace("sb://", "").rstrip("/")
    resource_uri = f"https://{namespace}/{queue_name}"

    # Build a short-lived SAS token
    expiry = int(time.time()) + ttl_seconds
    string_to_sign = urllib.parse.quote_plus(resource_uri) + "\n" + str(expiry)
    signature = b64encode(
        hmac.new(b64decode(key_value), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    sas_params = urllib.parse.urlencode({
        "sv":  "2019-02-02",
        "se":  str(expiry),
        "skn": key_name,
        "sig": signature,
    })

    return f"ServiceBus:{resource_uri}?{sas_params}"


# ---------------------------------------------------------------------------
# Graph client
# ---------------------------------------------------------------------------

class GraphClient:
    """Thin, synchronous wrapper around Microsoft Graph."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._msal = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._http = httpx.Client(timeout=30)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _token(self) -> str:
        result = self._msal.acquire_token_for_client(scopes=GRAPH_SCOPES)
        if "access_token" not in result:
            raise RuntimeError(
                f"MSAL token acquisition failed: {result.get('error_description', result)}"
            )
        return result["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _get(self, url: str, **kwargs) -> dict:
        r = self._http.get(url, headers=self._headers(), **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, json: dict) -> dict:
        r = self._http.post(url, headers=self._headers(), json=json)
        r.raise_for_status()
        return r.json()

    def _patch(self, url: str, json: dict) -> dict:
        r = self._http.patch(url, headers=self._headers(), json=json)
        r.raise_for_status()
        return r.json()

    def _delete(self, url: str) -> None:
        r = self._http.delete(url, headers=self._headers())
        if r.status_code not in (200, 204):
            r.raise_for_status()

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def list_subscriptions(self) -> list[dict]:
        return self._get(f"{GRAPH_BASE}/subscriptions").get("value", [])

    def create_subscription(
        self,
        mailbox_id: str,
        notification_url: str,
        client_state: str,
        change_types: str = "created,updated",
    ) -> dict:
        """
        Create a mail change-notification subscription for *mailbox_id*.

        *notification_url* should be either:
          - An HTTPS webhook URL  (requires public endpoint + validation handshake)
          - A ServiceBus: URI     (Graph pushes directly to ASB — no webhook needed)
            Built with asb_notification_url() from this module.
        """
        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=SUBSCRIPTION_TTL_MINUTES)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        sub = self._post(f"{GRAPH_BASE}/subscriptions", {
            "changeType":          change_types,
            "notificationUrl":     notification_url,
            "resource":            f"users/{mailbox_id}/mailFolders/inbox/messages",
            "expirationDateTime":  expiry,
            "clientState":         client_state,
        })
        logger.info("Created subscription %s for mailbox %s", sub["id"], mailbox_id)
        return sub

    def renew_subscription(self, subscription_id: str) -> dict:
        new_expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=SUBSCRIPTION_TTL_MINUTES)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        sub = self._patch(
            f"{GRAPH_BASE}/subscriptions/{subscription_id}",
            {"expirationDateTime": new_expiry},
        )
        logger.info("Renewed subscription %s → %s", subscription_id, new_expiry)
        return sub

    def delete_subscription(self, subscription_id: str) -> None:
        self._delete(f"{GRAPH_BASE}/subscriptions/{subscription_id}")
        logger.info("Deleted subscription %s", subscription_id)

    # ------------------------------------------------------------------
    # Delta query
    # ------------------------------------------------------------------

    def delta_messages(
        self,
        mailbox_id: str,
        delta_token: Optional[str] = None,
        top: int = 50,
    ) -> tuple[list[dict], Optional[str]]:
        """
        Run a delta query against the mailbox inbox.

        Returns (messages, new_delta_token).
        Store new_delta_token and pass it on the next call to get only changes.
        """
        if delta_token:
            url: Optional[str] = (
                f"{GRAPH_BASE}/users/{mailbox_id}"
                f"/mailFolders/inbox/messages/delta"
                f"?$deltaToken={delta_token}"
            )
        else:
            url = (
                f"{GRAPH_BASE}/users/{mailbox_id}"
                f"/mailFolders/inbox/messages/delta"
                f"?$top={top}"
                f"&$select=id,subject,from,receivedDateTime,body,bodyPreview"
            )

        messages: list[dict] = []
        new_delta_token: Optional[str] = None

        while url:
            try:
                page = self._get(url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 410:
                    logger.warning("Delta token expired for mailbox %s", mailbox_id)
                    return [], None
                raise

            for msg in page.get("value", []):
                if "@removed" not in msg:
                    messages.append(msg)

            url = page.get("@odata.nextLink")
            if "@odata.deltaLink" in page:
                link: str = page["@odata.deltaLink"]
                m = re.search(r"[?&]\$deltaToken=([^&]+)", link)
                new_delta_token = m.group(1) if m else link
                url = None

        logger.info("Delta query for %s → %d messages", mailbox_id, len(messages))
        return messages, new_delta_token

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def list_attachments(self, mailbox_id: str, message_id: str) -> list[dict]:
        """Return attachment metadata for a message (name, contentType, size, id)."""
        url = (
            f"{GRAPH_BASE}/users/{mailbox_id}/messages/{message_id}/attachments"
            f"?$select=id,name,contentType,size"
        )
        try:
            return self._get(url).get("value", [])
        except Exception as exc:
            logger.warning("Could not list attachments for message %s: %s", message_id, exc)
            return []

    def download_attachment(self, mailbox_id: str, message_id: str, attachment_id: str) -> bytes:
        """Download raw bytes for a single attachment."""
        url = (
            f"{GRAPH_BASE}/users/{mailbox_id}/messages/{message_id}"
            f"/attachments/{attachment_id}/$value"
        )
        r = self._http.get(url, headers=self._headers())
        r.raise_for_status()
        return r.content

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()
