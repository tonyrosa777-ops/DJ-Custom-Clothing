"""Vectorizer.ai API integration — JPEG/PNG bytes -> clean SVG bytes."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from pipeline import config

log = logging.getLogger(__name__)

VECTORIZER_ENDPOINT = "https://vectorizer.ai/api/v1/vectorize"
PHOTOPEA_FALLBACK_URL = "https://www.photopea.com/"
_REQUEST_TIMEOUT_SECONDS = 60.0


@dataclass
class VectorizerError(Exception):
    status: int
    message: str
    photopea_url: str = PHOTOPEA_FALLBACK_URL

    def __str__(self) -> str:
        return f"VectorizerError({self.status}): {self.message}"


async def vectorize(image_bytes: bytes, filename: str, content_type: str) -> bytes:
    """POST the image to Vectorizer.ai and return SVG bytes.

    Raises VectorizerError on any failure (missing creds, HTTP error, bad payload).
    """
    api_id, api_token = config.get_api_credentials()
    if not api_id or not api_token:
        raise VectorizerError(
            status=500,
            message="Vectorizer.ai credentials missing — set VECTORIZER_API_ID and VECTORIZER_API_TOKEN in .env.",
        )

    files = {"image": (filename, image_bytes, content_type or "application/octet-stream")}
    data = {"mode": "production", "output.file_format": "svg"}

    last_exception: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    VECTORIZER_ENDPOINT,
                    auth=httpx.BasicAuth(api_id, api_token),
                    files=files,
                    data=data,
                )
        except httpx.HTTPError as exc:
            last_exception = exc
            log.warning("Vectorizer.ai request failed on attempt %s: %s", attempt, exc)
            if attempt == 1:
                await asyncio.sleep(2)
                continue
            raise VectorizerError(status=502, message=f"Network error contacting Vectorizer.ai: {exc}") from exc

        if response.status_code == 200:
            svg_bytes = response.content
            if not svg_bytes or b"<svg" not in svg_bytes[:2000]:
                raise VectorizerError(
                    status=502,
                    message="Vectorizer.ai returned an empty or invalid SVG payload.",
                )
            return svg_bytes

        # Retry once on 5xx.
        if 500 <= response.status_code < 600 and attempt == 1:
            log.warning("Vectorizer.ai 5xx on attempt 1 (%s) — retrying.", response.status_code)
            await asyncio.sleep(2)
            continue

        # Non-retryable error: surface the response body for debugging.
        body_snippet = response.text[:500] if response.text else "(empty body)"
        log.error(
            "Vectorizer.ai error: status=%s body=%s", response.status_code, body_snippet
        )
        raise VectorizerError(
            status=502,
            message=f"Vectorizer.ai returned {response.status_code}: {body_snippet}",
        )

    # Should be unreachable — loop either returns or raises.
    raise VectorizerError(
        status=502,
        message=f"Vectorizer.ai request failed: {last_exception}",
    )
