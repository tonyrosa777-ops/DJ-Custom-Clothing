"""Real-ESRGAN AI upscaling via Replicate — recovers detail in tiny customer files.

Triggered automatically by main.py only when the post-intake image is below
both _TRIGGER_LONG_EDGE and _TRIGGER_SHORT_EDGE. Always fails open: any
exception, timeout, or missing credentials returns the original image
unchanged with was_upscaled=False so the pipeline never errors on this stage.

Uses direct httpx calls against Replicate's prediction API rather than the
official replicate-python SDK, which has a known httpx-Proxy import bug on
Python 3.13+ (ref: nexa-api.com 2026 bug report).
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging

import httpx
from PIL import Image

from pipeline import config

log = logging.getLogger(__name__)

_REPLICATE_PREDICTIONS_URL = (
    "https://api.replicate.com/v1/models/nightmareai/real-esrgan/predictions"
)
_REPLICATE_PREDICTION_BY_ID = "https://api.replicate.com/v1/predictions/{id}"

_OVERALL_TIMEOUT_SECONDS = 45.0   # absorbs Replicate cold-start variance
_HTTP_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 2.0

# Trigger: small AND skinny. The 1200x900 "okay but small" case must NOT fire.
_TRIGGER_LONG_EDGE = 1024
_TRIGGER_SHORT_EDGE = 800

# Above this post-upscale dimension, downsample back to cap memory.
_TARGET_LONG_EDGE = 2048

# Aggressive 4x upscale only on very small inputs; otherwise 2x is enough.
_AGGRESSIVE_THRESHOLD = 512


def should_upscale(image_size: tuple[int, int]) -> bool:
    """Pure threshold check — exposed for tests and for the cost-estimate log."""
    w, h = image_size
    return max(w, h) < _TRIGGER_LONG_EDGE and min(w, h) < _TRIGGER_SHORT_EDGE


async def maybe_upscale(image: Image.Image) -> tuple[Image.Image, bool]:
    """Return (upscaled_or_original, was_upscaled).

    Fail-open contract: this function never raises. If anything goes wrong
    (no token, kill-switch flipped, network error, timeout, unparseable
    response), it returns the input image unchanged with was_upscaled=False
    and logs the reason.
    """
    if not config.get_upscale_enabled():
        return image, False

    token = config.get_replicate_token()
    if not token:
        return image, False

    if not should_upscale(image.size):
        return image, False

    long_edge = max(image.size)
    scale = 4 if long_edge < _AGGRESSIVE_THRESHOLD else 2

    try:
        upscaled = await asyncio.wait_for(
            _call_replicate(image, scale, token),
            timeout=_OVERALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("Upscale skipped — Replicate exceeded %ss timeout.", _OVERALL_TIMEOUT_SECONDS)
        return image, False
    except Exception as exc:  # noqa: BLE001 — explicit fail-open
        log.warning("Upscale skipped — %s: %s", type(exc).__name__, exc)
        return image, False

    if max(upscaled.size) > _TARGET_LONG_EDGE:
        ratio = _TARGET_LONG_EDGE / max(upscaled.size)
        capped = (int(upscaled.size[0] * ratio), int(upscaled.size[1] * ratio))
        upscaled = upscaled.resize(capped, Image.LANCZOS)

    log.info(
        "Upscale ok: %sx%s -> %sx%s (scale=%s)",
        image.size[0], image.size[1], upscaled.size[0], upscaled.size[1], scale,
    )
    return upscaled, True


async def _call_replicate(image: Image.Image, scale: int, token: str) -> Image.Image:
    """Submit a Real-ESRGAN prediction and return the upscaled PIL.Image.

    Raises on any non-success path — `maybe_upscale` is the fail-open boundary.
    """
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # "Prefer: wait" makes Replicate hold the connection open until the
        # prediction finishes or 60s elapses. Most short jobs return inline;
        # cold-start jobs fall through to polling below.
        "Prefer": "wait",
    }
    body = {"input": {"image": data_url, "scale": scale}}

    async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS)) as client:
        response = await client.post(_REPLICATE_PREDICTIONS_URL, json=body, headers=headers)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Replicate POST returned {response.status_code}: {response.text[:300]}"
            )

        prediction = response.json()
        prediction_id = prediction.get("id")
        if not prediction_id:
            raise RuntimeError("Replicate response missing prediction id")

        while prediction.get("status") not in ("succeeded", "failed", "canceled"):
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            poll = await client.get(
                _REPLICATE_PREDICTION_BY_ID.format(id=prediction_id),
                headers={"Authorization": f"Bearer {token}"},
            )
            poll.raise_for_status()
            prediction = poll.json()

        status = prediction.get("status")
        if status != "succeeded":
            err = prediction.get("error") or prediction.get("logs", "")[:200] or "unknown"
            raise RuntimeError(f"Replicate prediction {status}: {err}")

        output = prediction.get("output")
        if isinstance(output, list):
            output = output[0] if output else None
        if not output:
            raise RuntimeError("Replicate prediction succeeded but output was empty")

        download = await client.get(output)
        download.raise_for_status()

    result = Image.open(io.BytesIO(download.content))
    result.load()  # force decode so the BytesIO can be GC'd
    if result.mode != "RGB":
        result = result.convert("RGB")
    return result
