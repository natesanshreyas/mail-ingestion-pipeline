"""
delta_token_store.py — Persists Graph delta-query tokens per mailbox
in Azure Blob Storage so worker pods retain state across restarts.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

logger = logging.getLogger(__name__)
CONTAINER_NAME = "delta-tokens"


class DeltaTokenStore:
    def __init__(self, connection_string: str) -> None:
        svc = BlobServiceClient.from_connection_string(connection_string)
        self._container = svc.get_container_client(CONTAINER_NAME)
        try:
            self._container.create_container()
        except Exception:
            pass  # already exists

    def get(self, mailbox_id: str) -> Optional[str]:
        blob = self._container.get_blob_client(f"{mailbox_id}.json")
        try:
            data = json.loads(blob.download_blob().readall())
            return data.get("deltaToken")
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            logger.warning("Could not read delta token for %s: %s", mailbox_id, exc)
            return None

    def set(self, mailbox_id: str, delta_token: str) -> None:
        blob = self._container.get_blob_client(f"{mailbox_id}.json")
        blob.upload_blob(
            json.dumps({"deltaToken": delta_token, "mailboxId": mailbox_id}),
            overwrite=True,
        )
