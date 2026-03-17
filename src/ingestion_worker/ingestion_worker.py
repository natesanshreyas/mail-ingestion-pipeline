"""
ingestion_worker.py — Consumes Graph change notifications from Azure
Service Bus, fetches new emails via delta query, classifies them with
OpenAI, and publishes the result to Azure Event Hubs.

Graph pushes notifications directly to the ASB queue (no public webhook
endpoint required). This service polls that queue continuously.

Endpoints:
  GET /health   liveness / readiness probe for k8s
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from openai import OpenAI
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.eventhub import EventHubProducerClient, EventData

sys.path.insert(0, "/app/shared")
from graph_client import GraphClient

from delta_token_store import DeltaTokenStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ingestion_worker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GRAPH_TENANT_ID     = os.environ["GRAPH_TENANT_ID"]
GRAPH_CLIENT_ID     = os.environ["GRAPH_CLIENT_ID"]
GRAPH_CLIENT_SECRET = os.environ["GRAPH_CLIENT_SECRET"]
GRAPH_CLIENT_STATE  = os.environ.get("GRAPH_CLIENT_STATE", "mail-ingestion-secret")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ASB_CONNECTION_STRING = os.environ["ASB_CONNECTION_STRING"]
ASB_QUEUE_NAME        = os.environ.get("ASB_QUEUE_NAME", "mail-notifications")

EVENTHUB_CONNECTION_STRING = os.environ["EVENTHUB_CONNECTION_STRING"]
EVENTHUB_NAME              = os.environ.get("EVENTHUB_NAME", "email-intents")

STORAGE_CONNECTION_STRING = os.environ["STORAGE_CONNECTION_STRING"]

# How long (seconds) to wait on an empty queue before polling again
ASB_MAX_WAIT_SECONDS = int(os.environ.get("ASB_MAX_WAIT_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

graph       = GraphClient(GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
eh_producer = EventHubProducerClient.from_connection_string(
    conn_str=EVENTHUB_CONNECTION_STRING, eventhub_name=EVENTHUB_NAME
)
delta_store = DeltaTokenStore(STORAGE_CONNECTION_STRING)

# ---------------------------------------------------------------------------
# ASB consumer loop (runs in a background thread)
# ---------------------------------------------------------------------------

_stop_event = threading.Event()


def _consume_loop() -> None:
    """Continuously receive messages from the ASB queue and process them."""
    logger.info("ASB consumer started (queue=%s)", ASB_QUEUE_NAME)
    with ServiceBusClient.from_connection_string(ASB_CONNECTION_STRING) as sb:
        with sb.get_queue_receiver(
            queue_name=ASB_QUEUE_NAME,
            max_wait_time=ASB_MAX_WAIT_SECONDS,
        ) as receiver:
            while not _stop_event.is_set():
                messages = receiver.receive_messages(max_message_count=10)
                for msg in messages:
                    try:
                        _handle_message(msg)
                        receiver.complete_message(msg)
                    except Exception as exc:
                        logger.error("Failed to process message, abandoning: %s", exc)
                        receiver.abandon_message(msg)


def _handle_message(msg: ServiceBusMessage) -> None:
    """Parse one Graph change notification and run the ingestion pipeline."""
    body = b"".join(msg.body) if hasattr(msg.body, "__iter__") else msg.body
    payload = json.loads(body)

    # Graph wraps notifications in {"value": [...]}
    notifications = payload.get("value", [payload])

    for notif in notifications:
        # Drop notifications with wrong clientState
        if notif.get("clientState") != GRAPH_CLIENT_STATE:
            logger.warning("Dropping notification with unexpected clientState")
            continue

        resource: str = notif.get("resource", "")
        mailbox_id = _mailbox_from_resource(resource)
        if not mailbox_id:
            logger.warning("Cannot determine mailboxId from resource: %s", resource)
            continue

        logger.info("Processing notification: mailbox=%s change=%s", mailbox_id, notif.get("changeType"))
        _process_mailbox(mailbox_id)


def _process_mailbox(mailbox_id: str) -> None:
    delta_token = delta_store.get(mailbox_id)
    messages, new_token = graph.delta_messages(mailbox_id, delta_token)

    if new_token is not None:
        delta_store.set(mailbox_id, new_token)
    elif delta_token:
        delta_store.set(mailbox_id, "")  # expired — reset for next full sync

    for msg in messages:
        result = _classify(msg, mailbox_id)
        _publish(result)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """\
You are an email analysis assistant. Given an email subject and body, extract:
1. name  — sender's full name (use signature/body if not clear from metadata)
2. email — sender's email address
3. intent — one concise sentence describing the sender's main request or purpose

Reply ONLY with valid JSON:
{"name": "<string or null>", "email": "<string or null>", "intent": "<string>"}
"""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").replace("\n", " ").strip()


def _classify(msg: dict, mailbox_id: str) -> dict:
    email_id    = msg.get("id", str(uuid.uuid4()))
    subject     = msg.get("subject", "(no subject)")
    sender_obj  = msg.get("from", {}).get("emailAddress", {})
    sender_name  = sender_obj.get("name", "")
    sender_email = sender_obj.get("address", "")
    received_at  = msg.get("receivedDateTime", "")
    body_obj     = msg.get("body", {})
    raw_body     = body_obj.get("content", "") or msg.get("bodyPreview", "")
    body_text    = _strip_html(raw_body) if body_obj.get("contentType") == "html" else raw_body

    user_msg = (
        f"Subject: {subject}\n"
        f"From: {sender_name} <{sender_email}>\n\n"
        f"Body:\n{body_text[:3000]}"
    )

    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        extracted: dict[str, Any] = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("LLM extraction failed for %s: %s", email_id, exc)
        extracted = {"name": sender_name, "email": sender_email, "intent": "extraction_failed"}

    return {
        "event_id":    str(uuid.uuid4()),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "email_id":    email_id,
        "mailbox_id":  mailbox_id,
        "subject":     subject,
        "received_at": received_at,
        "sender": {
            "name":  extracted.get("name") or sender_name,
            "email": extracted.get("email") or sender_email,
        },
        "intent": extracted.get("intent", ""),
        "model":  OPENAI_MODEL,
    }


# ---------------------------------------------------------------------------
# Event Hubs publish
# ---------------------------------------------------------------------------

def _publish(payload: dict) -> None:
    batch = eh_producer.create_batch()
    batch.add(EventData(json.dumps(payload)))
    eh_producer.send_batch(batch)
    logger.info(
        "Published: email=%s sender=%s intent=%r",
        payload["email_id"],
        payload["sender"]["email"],
        payload["intent"],
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _mailbox_from_resource(resource: str) -> str:
    parts = [p for p in resource.replace("\\", "/").split("/") if p]
    for i, part in enumerate(parts):
        if part.lower() in ("users", "user") and i + 1 < len(parts):
            return parts[i + 1]
    return ""


# ---------------------------------------------------------------------------
# FastAPI app (health probe only; real work happens in the background thread)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_consume_loop, daemon=True)
    t.start()
    logger.info("ASB consumer thread started")
    yield
    _stop_event.set()
    t.join(timeout=10)
    logger.info("ASB consumer thread stopped")


app = FastAPI(title="Mail Ingestion — Ingestion Worker", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "ingestion_worker"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8081)))
