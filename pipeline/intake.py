"""Universal file intake — JPEG, PNG, HEIC/HEIF, WebP, BMP, PDF -> clean PIL.Image.

HEIC support via pillow-heif (auto-registered with Pillow on import).
PDF support via pypdfium2 (renders the first page to a bitmap).
Everything else is handled by Pillow directly.

The output is a PIL.Image kept in memory through cleanup.clean_image and only
encoded to JPEG immediately before the Vectorizer.ai upload — eliminates the
double-lossy JPEG round-trip the previous version did.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import pillow_heif
import pypdfium2 as pdfium
from PIL import Image

pillow_heif.register_heif_opener()

log = logging.getLogger(__name__)

SUPPORTED_CONTENT_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/jpg", "image/pjpeg",
    "image/png",
    "image/heic", "image/heif",
    "image/webp",
    "image/bmp", "image/x-ms-bmp",
    "application/pdf",
})
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".pdf",
})

_PDF_RENDER_DPI = 150  # Vectorizer.ai doesn't need 300 DPI input — saves memory + time.
_JPEG_QUALITY = 95
_MAX_DIMENSION = 3000  # Cap raw input to bound RAM (Render free tier = 512 MB).


@dataclass
class IntakeError(Exception):
    status: int
    message: str

    def __str__(self) -> str:
        return f"IntakeError({self.status}): {self.message}"


@dataclass
class IntakeResult:
    """Decoded image + metadata flags from the intake stage."""
    image: Image.Image
    multipage_pdf: bool = False
    warnings: list[str] = field(default_factory=list)


def is_supported(content_type: str | None, filename: str | None) -> bool:
    """Check whether the uploaded file looks like something we can handle."""
    if content_type and content_type.lower() in SUPPORTED_CONTENT_TYPES:
        return True
    if filename:
        lowered = filename.lower()
        for ext in SUPPORTED_EXTENSIONS:
            if lowered.endswith(ext):
                return True
    return False


def _is_pdf(content_type: str | None, filename: str | None, payload: bytes) -> bool:
    if content_type and content_type.lower() == "application/pdf":
        return True
    if filename and filename.lower().endswith(".pdf"):
        return True
    return payload[:5] == b"%PDF-"


def _pdf_first_page_to_pil(payload: bytes) -> tuple[Image.Image, bool]:
    """Render page 1 of a PDF. Returns (pil_image, multipage_flag)."""
    try:
        pdf = pdfium.PdfDocument(payload)
    except Exception as exc:
        raise IntakeError(status=400, message=f"Could not read PDF: {exc}") from exc

    page_count = len(pdf)
    if page_count == 0:
        pdf.close()
        raise IntakeError(status=400, message="PDF has no pages.")

    page = pdf[0]
    scale = _PDF_RENDER_DPI / 72.0  # PDF native coords are 72 DPI.
    try:
        pil_image = page.render(scale=scale).to_pil()
    except Exception as exc:
        raise IntakeError(status=400, message=f"PDF render failed: {exc}") from exc
    finally:
        try:
            page.close()
        finally:
            pdf.close()

    return pil_image, page_count > 1


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Composite onto a white background so alpha channels become white, not black."""
    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        alpha = image.split()[-1]
        background.paste(image.convert("RGB"), mask=alpha)
        return background
    if image.mode == "P":
        # Palette image — may or may not have transparency.
        image = image.convert("RGBA")
        return _flatten_to_rgb(image)
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _maybe_downscale(image: Image.Image) -> Image.Image:
    w, h = image.size
    longest = max(w, h)
    if longest <= _MAX_DIMENSION:
        return image
    scale = _MAX_DIMENSION / longest
    new_size = (int(w * scale), int(h * scale))
    log.info("Intake downscale: %sx%s -> %sx%s", w, h, new_size[0], new_size[1])
    return image.resize(new_size, Image.LANCZOS)


def decode(
    payload: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> IntakeResult:
    """Decode any supported input format to a clean RGB PIL.Image.

    Returns an IntakeResult so callers can see metadata flags (multi-page PDF, etc.).
    """
    if not payload:
        raise IntakeError(status=400, message="Uploaded file is empty.")

    multipage_pdf = False

    if _is_pdf(content_type, filename, payload):
        image, multipage_pdf = _pdf_first_page_to_pil(payload)
    else:
        try:
            image = Image.open(io.BytesIO(payload))
            image.load()
        except Exception as exc:
            raise IntakeError(
                status=400,
                message="Unsupported or corrupt image — please upload a JPEG, PNG, HEIC, WebP, BMP, or PDF.",
            ) from exc

    # Respect EXIF orientation so portrait phone photos don't come in sideways.
    try:
        from PIL import ImageOps
        image = ImageOps.exif_transpose(image)
    except Exception as exc:
        log.warning("EXIF orientation failed: %s", exc)

    image = _flatten_to_rgb(image)
    image = _maybe_downscale(image)

    return IntakeResult(image=image, multipage_pdf=multipage_pdf)


def encode_jpeg(image: Image.Image) -> bytes:
    """Encode an in-memory PIL Image to JPEG bytes for the Vectorizer.ai upload."""
    if image.mode == "L":
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


def to_jpeg(
    payload: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> bytes:
    """Backwards-compatible bytes-in/bytes-out wrapper for callers that don't
    want to deal with PIL.Image hand-off. Internally calls decode() + encode_jpeg().
    """
    return encode_jpeg(decode(payload, filename, content_type).image)
