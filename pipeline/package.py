"""ZIP assembly + temp cleanup."""
from __future__ import annotations

import logging
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from pipeline.separate import ColorLayer

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_slug(value: str | None, default: str = "DJs_Art") -> str:
    if not value:
        return default
    cleaned = _SAFE_NAME_RE.sub("_", value.strip())
    cleaned = cleaned.strip("_")
    return cleaned or default


def pdf_filename(layer: ColorLayer) -> str:
    """Return the per-color PDF filename, e.g. Red_CC2222.pdf."""
    hex_part = layer.hex.lstrip("#").upper()
    safe_name = _safe_slug(layer.name, default=f"Color_{hex_part}")
    return f"{safe_name}_{hex_part}.pdf"


def _render_summary(layers: list[ColorLayer], job_name: str | None) -> str:
    lines: list[str] = []
    header = job_name.strip() if job_name and job_name.strip() else "DJ's Art Engine"
    lines.append(f"{header}")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Colors: {len(layers)}")
    lines.append("")
    lines.append("Name            Hex        Coverage")
    lines.append("-" * 40)
    for layer in layers:
        lines.append(
            f"{layer.name:<15} {layer.hex:<10} {layer.coverage_pct:>6.2f}%"
        )
    return "\n".join(lines) + "\n"


def build_zip(
    job_dir: Path,
    color_pdfs: list[tuple[ColorLayer, Path]],
    job_name: str | None,
) -> Path:
    """Create a ZIP containing one PDF per color plus color_summary.txt.

    Returns the path to the created ZIP (inside job_dir).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_slug = _safe_slug(job_name)
    zip_name = f"{zip_slug}_{timestamp}.zip"
    zip_path = job_dir / zip_name

    summary_text = _render_summary([layer for layer, _ in color_pdfs], job_name)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for layer, pdf_path in color_pdfs:
            arcname = pdf_filename(layer)
            zf.write(pdf_path, arcname=arcname)
        zf.writestr("color_summary.txt", summary_text)

    return zip_path


def cleanup(job_dir: Path) -> None:
    """Best-effort removal of the per-job temp directory."""
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:  # pragma: no cover
        log.warning("Cleanup failed for %s: %s", job_dir, exc)
