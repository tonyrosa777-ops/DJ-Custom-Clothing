"""Vectorizer.ai API integration — JPEG/PNG bytes -> clean SVG bytes."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from lxml import etree

from pipeline import config

log = logging.getLogger(__name__)

VECTORIZER_ENDPOINT = "https://vectorizer.ai/api/v1/vectorize"
PHOTOPEA_FALLBACK_URL = "https://www.photopea.com/"
_REQUEST_TIMEOUT_SECONDS = 60.0
_MAX_ATTEMPTS = 3
_BACKOFF_SCHEDULE = (2.0, 8.0)  # seconds before attempt 2, then attempt 3
_RETRY_AFTER_CAP_SECONDS = 30.0


@dataclass
class VectorizerError(Exception):
    status: int
    message: str
    photopea_url: str = PHOTOPEA_FALLBACK_URL

    def __str__(self) -> str:
        return f"VectorizerError({self.status}): {self.message}"


def _validate_svg(svg_bytes: bytes) -> None:
    """Raise VectorizerError if the response body isn't a parseable SVG document."""
    if not svg_bytes:
        raise VectorizerError(status=502, message="Vectorizer.ai returned an empty payload.")
    try:
        root = etree.fromstring(svg_bytes)
    except etree.XMLSyntaxError as exc:
        raise VectorizerError(
            status=502,
            message=f"Vectorizer.ai returned non-XML payload: {exc}",
        ) from exc
    tag = etree.QName(root).localname if isinstance(root.tag, str) else None
    if tag != "svg":
        raise VectorizerError(
            status=502,
            message=f"Vectorizer.ai returned XML root <{tag}>, expected <svg>.",
        )


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse a Retry-After header (seconds-only form; HTTP-date form ignored)."""
    if not header_value:
        return None
    try:
        seconds = float(header_value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, _RETRY_AFTER_CAP_SECONDS)


async def vectorize(image_bytes: bytes, filename: str, content_type: str) -> bytes:
    """POST the image to Vectorizer.ai and return SVG bytes.

    Retry policy:
      - up to 3 attempts total
      - on 5xx: exponential backoff (2s, then 8s)
      - on 429: respect Retry-After header (capped at 30s)
      - on network error: same backoff schedule
      - on any other 4xx (auth, bad request): no retry, surface body
    """
    api_id, api_token = config.get_api_credentials()
    if not api_id or not api_token:
        raise VectorizerError(
            status=500,
            message="Vectorizer.ai credentials missing — set VECTORIZER_API_ID and VECTORIZER_API_TOKEN in .env.",
        )

    files = {"image": (filename, image_bytes, content_type or "application/octet-stream")}
    data = {"mode": "production", "output.file_format": "svg"}

    last_error_message = "unknown error"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        backoff_for_next_attempt: float | None = None
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    VECTORIZER_ENDPOINT,
                    auth=httpx.BasicAuth(api_id, api_token),
                    files=files,
                    data=data,
                )
        except httpx.HTTPError as exc:
            last_error_message = f"network error: {exc}"
            log.warning("Vectorizer.ai network error on attempt %s: %s", attempt, exc)
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_SCHEDULE[attempt - 1])
                continue
            raise VectorizerError(status=502, message=last_error_message) from exc

        status = response.status_code

        if status == 200:
            svg_bytes = response.content
            _validate_svg(svg_bytes)  # raises VectorizerError on bad payload
            return svg_bytes

        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            log.warning("Vectorizer.ai 429 on attempt %s (Retry-After=%s)", attempt, retry_after)
            last_error_message = "Vectorizer.ai rate-limited (429)"
            if attempt < _MAX_ATTEMPTS:
                wait = retry_after if retry_after is not None else _BACKOFF_SCHEDULE[attempt - 1]
                await asyncio.sleep(wait)
                continue
            raise VectorizerError(status=502, message=last_error_message)

        if 500 <= status < 600:
            log.warning("Vectorizer.ai %s on attempt %s — retrying.", status, attempt)
            last_error_message = f"Vectorizer.ai server error {status}"
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_SCHEDULE[attempt - 1])
                continue
            raise VectorizerError(status=502, message=last_error_message)

        # Non-retryable error (4xx other than 429).
        body_snippet = response.text[:500] if response.text else "(empty body)"
        log.error("Vectorizer.ai error: status=%s body=%s", status, body_snippet)
        raise VectorizerError(
            status=502,
            message=f"Vectorizer.ai returned {status}: {body_snippet}",
        )

    # Defensive — loop should always return or raise.
    raise VectorizerError(status=502, message=last_error_message)
