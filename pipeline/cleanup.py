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

import logging

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

log = logging.getLogger(__name__)

_CONTRAST_FACTOR = 1.5
_SHARPEN_PASSES = 2

# If the mean saturation of non-near-black pixels is below this (0..255),
# treat the logo as effectively monochrome and collapse to grayscale before
# sending to the vectorizer. 30/255 ≈ 12% saturation — a safe threshold that
# catches "black on white with JPEG artifacts" without touching real color.
_SATURATION_MEAN_THRESHOLD = 30.0
# Fraction of pixels with saturation > 40 (moderately saturated). Was 0.05;
# lowered to 0.02 because logos where the color region is geometrically small
# relative to whitespace + black text (e.g., a brand logo with a small orange
# accent on a white field) sit between 2% and 5% and were being miscategorized.
_SATURATION_COVERAGE_THRESHOLD = 0.02
# Third signal — fraction of pixels with saturation > 180 (strongly saturated).
# This catches "tiny but unambiguous color region" cases that the mean and
# moderate-coverage checks can miss. Even a 0.3% pocket of strong color is
# strong evidence of intentional color the user wants printed. JPEG chroma
# noise on truly monochrome scans rarely produces sat > 180.
_SATURATION_STRONG_FRACTION_THRESHOLD = 0.003

# fastNlMeansDenoising parameters. h controls strength — lower preserves detail,
# higher smooths harder. 7 is a conservative default for scanned/photographed logos.
_DENOISE_H = 7
_DENOISE_TEMPLATE_WINDOW = 7
_DENOISE_SEARCH_WINDOW = 21
# Cap input dim before denoising — fastNlMeansDenoising is O(n · template² · search²),
# 30+ seconds on a 6000px input on Render's CPU. 1500px keeps it under 5 seconds.
_DENOISE_MAX_DIMENSION = 1500


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
    strong_sat_fraction = float((saturation[mask] > 180).sum()) / float(mask.sum())

    log.info(
        "Cleanup saturation: mean=%.2f high_sat=%.3f strong_sat=%.3f thresholds=(%.1f, %.3f, %.3f)",
        mean_sat, high_sat_fraction, strong_sat_fraction,
        _SATURATION_MEAN_THRESHOLD,
        _SATURATION_COVERAGE_THRESHOLD,
        _SATURATION_STRONG_FRACTION_THRESHOLD,
    )
    # Classify as monochrome ONLY when all three saturation signals agree.
    # Any single strong color region (3rd condition) is enough to force
    # color-mode processing — protects logos like the Optimus mark where
    # the orange is small but unambiguous.
    return (
        mean_sat < _SATURATION_MEAN_THRESHOLD
        and high_sat_fraction < _SATURATION_COVERAGE_THRESHOLD
        and strong_sat_fraction < _SATURATION_STRONG_FRACTION_THRESHOLD
    )


def _denoise(image: Image.Image) -> Image.Image:
    """Apply fastNlMeansDenoising — single-channel for L mode, colored for RGB.

    Skips denoising entirely when the image is smaller than the template window
    (e.g. 1×1 favicons), since OpenCV would error. For oversized inputs the
    image is downscaled first to keep runtime bounded, then upscaled back.
    """
    w, h = image.size
    min_required = _DENOISE_TEMPLATE_WINDOW
    if w < min_required or h < min_required:
        log.info("Skipping denoise — image %sx%s smaller than template window %s.",
                 w, h, min_required)
        return image

    # Downscale before denoising if the image is huge.
    longest = max(w, h)
    needs_downscale = longest > _DENOISE_MAX_DIMENSION
    if needs_downscale:
        scale = _DENOISE_MAX_DIMENSION / longest
        small_size = (max(int(w * scale), 1), max(int(h * scale), 1))
        log.info("Denoise downscale: %sx%s -> %sx%s before denoising.",
                 w, h, small_size[0], small_size[1])
        small = image.resize(small_size, Image.LANCZOS)
        denoised_small = _denoise_core(small)
        return denoised_small.resize((w, h), Image.LANCZOS)

    return _denoise_core(image)


def _denoise_core(image: Image.Image) -> Image.Image:
    """Run OpenCV fastNlMeansDenoising on the image at its current size."""
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


def clean_image(image: Image.Image) -> Image.Image:
    """Run the full pre-vectorization cleanup pipeline.

    Takes and returns an in-memory PIL.Image — no JPEG round-trip. Caller
    should pass through to vectorize.vectorize via intake.encode_jpeg().
    """
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

    return image
