"""Universal file intake — JPEG, PNG, HEIC/HEIF, WebP, BMP, PDF -> clean JPEG.

HEIC support via pillow-heif (auto-registered with Pillow on import).
PDF support via pypdfium2 (renders the first page to a bitmap).
Everything else is handled by Pillow directly.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

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

_PDF_RENDER_DPI = 300
_JPEG_QUALITY = 95
_MAX_DIMENSION = 8000  # Downscale absurdly large inputs to keep pipeline snappy.


@dataclass
class IntakeError(Exception):
    status: int
    message: str

    def __str__(self) -> str:
        return f"IntakeError({self.status}): {self.message}"


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


def _pdf_first_page_to_pil(payload: bytes) -> Image.Image:
    """Render the first page of a PDF to a PIL Image at _PDF_RENDER_DPI."""
    try:
        pdf = pdfium.PdfDocument(payload)
    except Exception as exc:
        raise IntakeError(status=400, message=f"Could not read PDF: {exc}") from exc

    if len(pdf) == 0:
        raise IntakeError(status=400, message="PDF has no pages.")

    page = pdf[0]
    scale = _PDF_RENDER_DPI / 72.0  # PDF native coords are 72 DPI.
    try:
        pil_image = page.render(scale=scale).to_pil()
    except Exception as exc:
        raise IntakeError(status=400, message=f"PDF render failed: {exc}") from exc
    finally:
        page.close()
        pdf.close()

    return pil_image


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


def to_jpeg(
    payload: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> bytes:
    """Convert any supported input format to a clean RGB JPEG and return its bytes."""
    if not payload:
        raise IntakeError(status=400, message="Uploaded file is empty.")

    if _is_pdf(content_type, filename, payload):
        image = _pdf_first_page_to_pil(payload)
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
    except Exception:  # pragma: no cover
        pass

    image = _flatten_to_rgb(image)
    image = _maybe_downscale(image)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True, subsampling=0)
    return buffer.getvalue()
