"""Pre-vectorization image cleanup.

Pipeline (applied in this order to a clean RGB JPEG from intake):

  1. Saturation analysis -> decide grayscale vs keep-color
  2. Noise reduction (OpenCV fastNlMeansDenoising) — FIRST so we don't
     amplify compression artifacts in later steps
  3. Auto-levels (ImageOps.autocontrast) — set the tonal range cleanly
  4. Contrast enhancement (PIL ImageEnhance.Contrast, factor 1.5)
  5. Sharpening (PIL ImageFilter.SHARPEN, two passes) — LAST so sharpening
     adds crispness to a clean image rather than amplifying noise

Only collapses to grayscale if saturation analysis confirms the logo is
effectively monochrome — most DJ jobs (full-color logos) stay in color.
"""
from __future__ import annotations

import io
import logging

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

log = logging.getLogger(__name__)

_CONTRAST_FACTOR = 1.5
_SHARPEN_PASSES = 2
_JPEG_QUALITY = 95

# If the mean saturation of non-near-black pixels is below this (0..255),
# treat the logo as effectively monochrome and collapse to grayscale before
# sending to the vectorizer. 30/255 ≈ 12% saturation — a safe threshold that
# catches "black on white with JPEG artifacts" without touching real color.
_SATURATION_MEAN_THRESHOLD = 30.0
_SATURATION_COVERAGE_THRESHOLD = 0.05  # Fraction of pixels with saturation > 40.

# fastNlMeansDenoising parameters. h controls strength — lower preserves detail,
# higher smooths harder. 7 is a conservative default for scanned/photographed logos.
_DENOISE_H = 7
_DENOISE_TEMPLATE_WINDOW = 7
_DENOISE_SEARCH_WINDOW = 21


def _is_effectively_monochrome(image: Image.Image) -> bool:
    """Decide whether the logo has enough color to matter.

    Converts to HSV and measures saturation on non-near-black pixels. Near-black
    is ignored so that an image of "black text on white" isn't dragged toward
    color by the few anti-aliased gray pixels with noisy chroma.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] != 3:
        return True  # Already single-channel.

    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    # Ignore near-black pixels (noisy saturation at low value).
    mask = value > 20
    if not mask.any():
        return True

    mean_sat = float(saturation[mask].mean())
    high_sat_fraction = float((saturation[mask] > 40).sum()) / float(mask.sum())

    log.info(
        "Cleanup saturation: mean=%.2f high_sat_fraction=%.3f thresholds=(%.1f, %.3f)",
        mean_sat, high_sat_fraction,
        _SATURATION_MEAN_THRESHOLD, _SATURATION_COVERAGE_THRESHOLD,
    )
    return (
        mean_sat < _SATURATION_MEAN_THRESHOLD
        and high_sat_fraction < _SATURATION_COVERAGE_THRESHOLD
    )


def _denoise(image: Image.Image) -> Image.Image:
    """Apply fastNlMeansDenoising — single-channel for L mode, colored for RGB."""
    arr = np.asarray(image)
    if image.mode == "L":
        denoised = cv2.fastNlMeansDenoising(
            arr,
            None,
            h=_DENOISE_H,
            templateWindowSize=_DENOISE_TEMPLATE_WINDOW,
            searchWindowSize=_DENOISE_SEARCH_WINDOW,
        )
        return Image.fromarray(denoised, mode="L")

    # RGB path — OpenCV expects BGR.
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    denoised_bgr = cv2.fastNlMeansDenoisingColored(
        bgr,
        None,
        h=_DENOISE_H,
        hColor=_DENOISE_H,
        templateWindowSize=_DENOISE_TEMPLATE_WINDOW,
        searchWindowSize=_DENOISE_SEARCH_WINDOW,
    )
    return Image.fromarray(cv2.cvtColor(denoised_bgr, cv2.COLOR_BGR2RGB), mode="RGB")


def clean_image(jpeg_bytes: bytes) -> bytes:
    """Run the full pre-vectorization cleanup pipeline and return JPEG bytes."""
    image = Image.open(io.BytesIO(jpeg_bytes))
    image.load()
    if image.mode != "RGB":
        image = image.convert("RGB")

    # 1. Saturation analysis -> optionally collapse to grayscale.
    monochrome = _is_effectively_monochrome(image)
    if monochrome:
        log.info("Cleanup: image classified as monochrome, converting to grayscale.")
        image = image.convert("L")
    else:
        log.info("Cleanup: image classified as color, keeping RGB.")

    # 2. Noise reduction FIRST (before anything that amplifies high-frequency content).
    image = _denoise(image)

    # 3. Auto-levels — stretch histogram to full 0-255 range on the clean image.
    image = ImageOps.autocontrast(image, cutoff=0)

    # 4. Contrast enhancement.
    image = ImageEnhance.Contrast(image).enhance(_CONTRAST_FACTOR)

    # 5. Sharpening LAST — two passes.
    for _ in range(_SHARPEN_PASSES):
        image = image.filter(ImageFilter.SHARPEN)

    # Vectorizer.ai accepts JPEG regardless of mode; but ensure we save as JPEG.
    if image.mode == "L":
        # JPEG will save an L-mode image as grayscale JPEG — preferred over RGB
        # for genuinely monochrome logos so the vectorizer sees one channel.
        save_kwargs = {"format": "JPEG", "quality": _JPEG_QUALITY, "optimize": True}
    else:
        if image.mode != "RGB":
            image = image.convert("RGB")
        save_kwargs = {
            "format": "JPEG",
            "quality": _JPEG_QUALITY,
            "optimize": True,
            "subsampling": 0,
        }

    buffer = io.BytesIO()
    image.save(buffer, **save_kwargs)
    return buffer.getvalue()
