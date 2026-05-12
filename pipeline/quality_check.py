"""Claude vision quality check — second opinion on what's in the customer's image.

Runs in parallel with the upscale stage so latency overlaps. Returns a
structured QualityVerdict. Fails open: any error returns a permissive default
verdict (printable=True, no warnings) so the pipeline never errors because of
this stage. Cost ~$0.001-0.005 per call on claude-haiku-4-5.

Uses direct httpx against api.anthropic.com rather than the anthropic-python
SDK — keeps the dependency surface flat and avoids one more package to track.

Bounding boxes from Claude are returned in resized-image coordinates per the
Anthropic vision docs. We rescale them back to the source image's coordinates
before exposing on QualityVerdict, so main.py's crop step gets pixel-accurate
coordinates against the working image.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass, field
from typing import Literal

import httpx
from PIL import Image

from pipeline import config

log = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_OVERALL_TIMEOUT_SECONDS = 20.0
_HTTP_TIMEOUT_SECONDS = 18.0
_QC_MAX_DIMENSION = 1568  # matches Claude's native cap for non-Opus models
_QC_JPEG_QUALITY = 85
_QC_MAX_TOKENS = 1024

VerdictCategory = Literal[
    "ok",
    "low_res",
    "not_a_logo",
    "photo_of_object",
    "illegible_text",
    "gradients",
    "unknown",
]


@dataclass
class QualityVerdict:
    """Structured assessment of the input image's print-readiness."""
    printable: bool = True
    verdict_category: VerdictCategory = "unknown"
    dj_message: str = ""
    customer_ask: str = ""
    dominant_colors: list[str] = field(default_factory=list)
    is_photo_of_object: bool = False
    logo_bbox: tuple[int, int, int, int] | None = None  # in source image coords
    has_gradients: bool = False
    has_illegible_text: bool = False
    model_used: str = ""


_PERMISSIVE_DEFAULT = QualityVerdict()


_TOOL_SCHEMA = {
    "name": "report_quality_verdict",
    "description": (
        "Report your assessment of whether this image is suitable for "
        "screen-printing as a logo on apparel."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "printable": {
                "type": "boolean",
                "description": (
                    "True if this image can produce print-ready color separations. "
                    "False ONLY when the image is clearly not a logo "
                    "(screenshot of a webpage, random photo, text document) or "
                    "is unrecoverably bad (heavily blurred to the point of being "
                    "unidentifiable). Low resolution alone is NOT a reason to "
                    "mark unprintable — small logos can still be upscaled."
                ),
            },
            "verdict_category": {
                "type": "string",
                "enum": [
                    "ok",
                    "low_res",
                    "not_a_logo",
                    "photo_of_object",
                    "illegible_text",
                    "gradients",
                    "unknown",
                ],
                "description": "Primary category for routing the UI banner.",
            },
            "dj_message": {
                "type": "string",
                "description": (
                    "Short one-sentence message for the shop owner (DJ) about "
                    "what is wrong with THIS image specifically. Must reference "
                    "concrete details from the image — never use boilerplate "
                    "like 'image rejected' or 'try a different file'. "
                    "Examples of good messages: 'This is a screenshot of a "
                    "Google search result, not a logo.' / 'The text in this "
                    "logo is too small to read at print size.' Required when "
                    "printable is false; can be empty when printable is true."
                ),
            },
            "customer_ask": {
                "type": "string",
                "description": (
                    "Copy-pasteable sentence the shop owner can text/email to "
                    "the customer to request a usable file. Must name specific "
                    "file types (.AI, .EPS, .SVG, .PDF vector preferred; "
                    "otherwise PNG) AND specify a minimum resolution number "
                    "(at least 2000 pixels on the long side). Must NOT be "
                    "generic like 'send a better file'. Required when "
                    "printable is false; can be empty when printable is true."
                ),
            },
            "dominant_colors": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Up to 8 dominant hex color codes you can see in the "
                    "image (e.g. '#FF0000'). Exclude pure-white backgrounds. "
                    "Used as a cross-check against the vectorizer's color list."
                ),
            },
            "is_photo_of_object": {
                "type": "boolean",
                "description": (
                    "True if this is a photograph of a physical object with the "
                    "logo on it — e.g. a phone snapshot of an existing shirt, "
                    "business card, mug, sign. False for a flat digital file."
                ),
            },
            "logo_bbox": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Tight bounding box around the logo in pixel coordinates "
                    "[x1, y1, x2, y2], where (x1, y1) is the top-left and "
                    "(x2, y2) is the bottom-right. Only fill this when "
                    "is_photo_of_object is true. Coordinates are with respect "
                    "to the image as you see it."
                ),
            },
            "has_gradients": {
                "type": "boolean",
                "description": "True if the logo uses smooth color gradients.",
            },
            "has_illegible_text": {
                "type": "boolean",
                "description": (
                    "True if there is small text in the logo that is unreadable "
                    "at the current resolution and will burn poorly on screen."
                ),
            },
        },
        "required": [
            "printable",
            "verdict_category",
            "dj_message",
            "customer_ask",
            "dominant_colors",
            "is_photo_of_object",
            "has_gradients",
            "has_illegible_text",
        ],
    },
}


_USER_PROMPT = (
    "You are reviewing an image uploaded to a screen-printing shop's automation "
    "pipeline. The shop owner (DJ) will produce solid-color transparency films "
    "from this image to burn screens for shirt printing. Be permissive — small "
    "or compressed logos can still be processed because the pipeline upscales "
    "and vectorizes. Reject only when the image is clearly NOT a logo "
    "(screenshot, random photo, document) or is unrecoverably damaged.\n\n"
    "Use the report_quality_verdict tool to return your assessment. If you "
    "mark printable=false, your dj_message and customer_ask must be specific "
    "to what you actually see in this image — no boilerplate."
)


async def assess(image: Image.Image) -> QualityVerdict:
    """Send the image to Claude vision and return a structured verdict.

    Fail-open contract: never raises. On any error, missing credentials, or
    unparseable response, returns the permissive default verdict and logs.
    """
    api_key = config.get_anthropic_key()
    if not api_key:
        return _PERMISSIVE_DEFAULT

    model = config.get_qc_model()
    source_size = image.size

    try:
        verdict = await asyncio.wait_for(
            _call_anthropic(image, api_key, model, source_size),
            timeout=_OVERALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("Quality check skipped — Anthropic exceeded %ss.", _OVERALL_TIMEOUT_SECONDS)
        return _PERMISSIVE_DEFAULT
    except Exception as exc:  # noqa: BLE001 — explicit fail-open
        log.warning("Quality check skipped — %s: %s", type(exc).__name__, exc)
        return _PERMISSIVE_DEFAULT

    log.info(
        "QC: printable=%s category=%s photo=%s gradients=%s illegible=%s",
        verdict.printable,
        verdict.verdict_category,
        verdict.is_photo_of_object,
        verdict.has_gradients,
        verdict.has_illegible_text,
    )
    return verdict


async def _call_anthropic(
    image: Image.Image,
    api_key: str,
    model: str,
    source_size: tuple[int, int],
) -> QualityVerdict:
    """One-shot Anthropic Messages API call with tool-use enforcement."""
    qc_image, qc_size = _prepare_qc_image(image)
    buf = io.BytesIO()
    qc_image.save(buf, format="JPEG", quality=_QC_JPEG_QUALITY, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    body = {
        "model": model,
        "max_tokens": _QC_MAX_TOKENS,
        "tools": [_TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": "report_quality_verdict"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _USER_PROMPT},
                ],
            }
        ],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS)) as client:
        response = await client.post(_ANTHROPIC_URL, json=body, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(
                f"Anthropic returned {response.status_code}: {response.text[:300]}"
            )
        data = response.json()

    return _parse_tool_response(data, model, source_size, qc_size)


def _prepare_qc_image(image: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    """Resize for Claude's native resolution cap; return (image, size_used)."""
    longest = max(image.size)
    if longest <= _QC_MAX_DIMENSION:
        return image, image.size
    scale = _QC_MAX_DIMENSION / longest
    new_size = (int(image.size[0] * scale), int(image.size[1] * scale))
    return image.resize(new_size, Image.LANCZOS), new_size


def _parse_tool_response(
    data: dict,
    model: str,
    source_size: tuple[int, int],
    qc_size: tuple[int, int],
) -> QualityVerdict:
    """Extract the tool_use input from a successful Messages API response."""
    content = data.get("content") or []
    tool_block = next((b for b in content if b.get("type") == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Anthropic response had no tool_use block")
    payload = tool_block.get("input") or {}
    if not isinstance(payload, dict):
        raise RuntimeError("Anthropic tool_use input was not an object")

    bbox = payload.get("logo_bbox")
    rescaled_bbox = _rescale_bbox(bbox, qc_size, source_size) if bbox else None

    # Enforce the failure-UX contract at the parse boundary: if Claude marked
    # this unprintable but returned empty strings, downgrade to a permissive
    # verdict with a logged warning. Better to let the job proceed than to
    # show DJ an empty red banner.
    printable = bool(payload.get("printable", True))
    dj_message = (payload.get("dj_message") or "").strip()
    customer_ask = (payload.get("customer_ask") or "").strip()
    if not printable and (len(dj_message) < 10 or len(customer_ask) < 10):
        log.warning(
            "QC returned unprintable verdict with insufficient messaging "
            "(dj_message=%r customer_ask=%r) — downgrading to permissive.",
            dj_message, customer_ask,
        )
        printable = True

    verdict_category_raw = payload.get("verdict_category") or "unknown"
    allowed_categories = set(_TOOL_SCHEMA["input_schema"]["properties"]["verdict_category"]["enum"])
    verdict_category: VerdictCategory = (
        verdict_category_raw if verdict_category_raw in allowed_categories else "unknown"
    )  # type: ignore[assignment]

    dominant_colors = payload.get("dominant_colors") or []
    if not isinstance(dominant_colors, list):
        dominant_colors = []
    dominant_colors = [str(c) for c in dominant_colors if isinstance(c, str)][:8]

    return QualityVerdict(
        printable=printable,
        verdict_category=verdict_category,
        dj_message=dj_message,
        customer_ask=customer_ask,
        dominant_colors=dominant_colors,
        is_photo_of_object=bool(payload.get("is_photo_of_object", False)),
        logo_bbox=rescaled_bbox,
        has_gradients=bool(payload.get("has_gradients", False)),
        has_illegible_text=bool(payload.get("has_illegible_text", False)),
        model_used=model,
    )


def _rescale_bbox(
    bbox: list[int] | tuple[int, ...],
    qc_size: tuple[int, int],
    source_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    """Map a bbox from QC-resized coordinates back to the source image."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    qc_w, qc_h = qc_size
    src_w, src_h = source_size
    if qc_w <= 0 or qc_h <= 0:
        return None
    rx = src_w / qc_w
    ry = src_h / qc_h
    sx1 = max(0, min(src_w, int(x1 * rx)))
    sy1 = max(0, min(src_h, int(y1 * ry)))
    sx2 = max(0, min(src_w, int(x2 * rx)))
    sy2 = max(0, min(src_h, int(y2 * ry)))
    if sx2 <= sx1 or sy2 <= sy1:
        return None
    return sx1, sy1, sx2, sy2
