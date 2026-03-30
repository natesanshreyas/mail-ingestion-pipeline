"""
content_understanding_client.py — Thin Azure Content Understanding client.

Submits a document (PDF or image bytes) to an Azure CU analyzer and polls
for the result. Returns extracted fields as a plain dict.

Used by the ingestion worker when AZURE_CU_ENDPOINT is configured.
Only invoked when an email has attachments — completely skipped otherwise.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# How long to poll for a CU result before giving up (seconds)
_POLL_TIMEOUT = 120
_POLL_INTERVAL = 3


class ContentUnderstandingError(Exception):
    pass


class ContentUnderstandingClient:
    """Synchronous client for Azure AI Content Understanding."""

    def __init__(self, endpoint: str, api_key: str, analyzer_id: str) -> None:
        """
        Args:
            endpoint:    Azure CU endpoint, e.g.
                         https://<resource>.cognitiveservices.azure.com
            api_key:     Ocp-Apim-Subscription-Key or Azure AD bearer token
            analyzer_id: The CU analyzer to use (e.g. "prebuilt-read" or a
                         custom analyzer you created for ACORD/loss-run forms)
        """
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._analyzer_id = analyzer_id
        self._http = httpx.Client(timeout=60)

    def _headers(self) -> dict[str, str]:
        return {
            "Ocp-Apim-Subscription-Key": self._api_key,
            "Content-Type": "application/json",
        }

    def analyze_bytes(
        self,
        content: bytes,
        content_type: str = "application/pdf",
        filename: str = "attachment",
    ) -> dict[str, Any]:
        """
        Submit document bytes to CU and return extracted fields.

        Returns a dict like:
          {
            "analyzer_id": "prebuilt-read",
            "fields": {"field_name": {"value": ..., "confidence": 0.99}, ...},
            "markdown": "# Document\n...",   # full text if available
          }

        Raises ContentUnderstandingError on failure.
        """
        import base64

        url = (
            f"{self._endpoint}/contentunderstanding/analyzers"
            f"/{self._analyzer_id}:analyze?api-version=2024-12-01-preview"
        )

        body = {
            "url": None,
            "base64Source": base64.b64encode(content).decode("utf-8"),
        }

        resp = self._http.post(url, headers=self._headers(), json=body)
        if resp.status_code not in (200, 202):
            raise ContentUnderstandingError(
                f"CU submit failed ({resp.status_code}): {resp.text[:400]}"
            )

        # 202 Accepted — poll the operation URL
        if resp.status_code == 202:
            operation_url = resp.headers.get("Operation-Location") or resp.headers.get("operation-location")
            if not operation_url:
                raise ContentUnderstandingError("CU returned 202 but no Operation-Location header")
            result = self._poll(operation_url)
        else:
            result = resp.json()

        return self._extract_fields(result)

    def _poll(self, operation_url: str) -> dict[str, Any]:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL)
            resp = self._http.get(operation_url, headers=self._headers())
            if resp.status_code >= 400:
                raise ContentUnderstandingError(
                    f"CU poll failed ({resp.status_code}): {resp.text[:400]}"
                )
            data = resp.json()
            status = data.get("status", "").lower()
            if status == "succeeded":
                return data
            if status in ("failed", "canceled"):
                raise ContentUnderstandingError(f"CU operation {status}: {data}")
            # still running — keep polling
        raise ContentUnderstandingError(
            f"CU timed out after {_POLL_TIMEOUT}s waiting for result"
        )

    def _extract_fields(self, result: dict[str, Any]) -> dict[str, Any]:
        """Flatten CU result into a simple {field: {value, confidence}} dict."""
        fields: dict[str, Any] = {}

        # Navigate to fields depending on CU response shape
        analyze_result = result.get("analyzeResult") or result.get("result") or result
        contents = analyze_result.get("contents", [])

        for content_item in contents:
            for field_name, field_data in (content_item.get("fields") or {}).items():
                if isinstance(field_data, dict):
                    fields[field_name] = {
                        "value": field_data.get("valueString")
                              or field_data.get("value")
                              or field_data.get("content"),
                        "confidence": field_data.get("confidence", 0.0),
                    }

        # Pull markdown/text if available
        markdown = ""
        for content_item in contents:
            markdown += content_item.get("markdown", "") or content_item.get("content", "")

        return {
            "analyzer_id": self._analyzer_id,
            "fields": fields,
            "markdown": markdown[:5000],  # cap for downstream consumers
        }

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ContentUnderstandingClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()
