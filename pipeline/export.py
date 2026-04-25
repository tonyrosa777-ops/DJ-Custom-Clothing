"""Per-color separation PDF generation.

For each target color in the vectorized SVG, produce a grayscale PDF where
every pixel matching the target is BLACK (ink) and everything else is WHITE
(clear). Rendered via svglib -> Pillow -> reportlab at the configured DPI,
with a 0.125" bleed on all sides.
"""
from __future__ import annotations

import copy
import io
import re
from pathlib import Path

from lxml import etree
from PIL import Image
from reportlab.graphics import renderPM
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from svglib.svglib import svg2rlg

from pipeline.color_math import colors_match, normalize_hex

_SVG_NS = "http://www.w3.org/2000/svg"
_STYLE_FILL_RE = re.compile(r"fill\s*:\s*(#[0-9a-fA-F]{3,6}|none|[a-zA-Z]+)")
_STYLE_STROKE_RE = re.compile(r"stroke\s*:\s*(#[0-9a-fA-F]{3,6}|none|[a-zA-Z]+)")
_STYLE_STROKE_ANY_RE = re.compile(r"stroke\s*:\s*([^;]+)")

_POINTS_PER_INCH = 72.0
_SVG_DEFAULT_USER_UNITS_PER_INCH = 96.0  # SVG spec default if no explicit units.


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _parse_length_to_inches(value: str, viewbox_size: float) -> float:
    """Convert an SVG length string to inches, using viewbox as fallback reference."""
    if not value:
        return viewbox_size / _SVG_DEFAULT_USER_UNITS_PER_INCH
    value = value.strip()
    match = re.match(r"^([-+]?\d*\.?\d+)\s*([a-zA-Z%]*)$", value)
    if not match:
        return viewbox_size / _SVG_DEFAULT_USER_UNITS_PER_INCH
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit in ("", "px"):
        return number / _SVG_DEFAULT_USER_UNITS_PER_INCH
    if unit == "in":
        return number
    if unit == "cm":
        return number / 2.54
    if unit == "mm":
        return number / 25.4
    if unit == "pt":
        return number / 72.0
    if unit == "pc":
        return number / 6.0
    # % or unknown -> assume viewbox mapping.
    return viewbox_size / _SVG_DEFAULT_USER_UNITS_PER_INCH


def _parse_viewbox(root) -> tuple[float, float, float, float]:
    vb = root.get("viewBox")
    if vb:
        parts = vb.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return tuple(float(p) for p in parts)  # type: ignore[return-value]
            except ValueError:
                pass
    # No viewBox: fall back to width/height as pixel dimensions.
    width = root.get("width", "100")
    height = root.get("height", "100")

    def _strip_unit(v: str) -> float:
        m = re.match(r"^([-+]?\d*\.?\d+)", v.strip())
        return float(m.group(1)) if m else 100.0

    return 0.0, 0.0, _strip_unit(width), _strip_unit(height)


def _resolve_paint(element, attr: str, style_re: re.Pattern[str]) -> str | None:
    """Resolve effective paint color (#RRGGBB) from attribute or inline style."""
    raw = element.get(attr)
    if raw:
        if raw.strip().lower() == "none":
            return None
        normalized = normalize_hex(raw)
        if normalized:
            return normalized

    style = element.get("style")
    if style:
        match = style_re.search(style)
        if match:
            return normalize_hex(match.group(1))
    return None


def _rewrite_fill(element, target_hex: str) -> None:
    """Recolor this element so target-hex paint becomes black, everything else white.

    Both fill and stroke are inspected. If the fill matches the target color
    the fill becomes black; otherwise white. If the stroke matches the target
    the stroke is preserved at black; otherwise the stroke is dropped to none.
    This means stroked-only paths still render their lines on the right layer.
    """
    fill_color = _resolve_paint(element, "fill", _STYLE_FILL_RE)
    stroke_color = _resolve_paint(element, "stroke", _STYLE_STROKE_RE)

    fill_matches = bool(fill_color) and colors_match(fill_color, target_hex)
    stroke_matches = bool(stroke_color) and colors_match(stroke_color, target_hex)

    new_fill = "#000000" if fill_matches else "#FFFFFF"
    element.set("fill", new_fill)

    if stroke_matches:
        element.set("stroke", "#000000")
    else:
        element.set("stroke", "none")

    # Strip style fill/stroke so they can't override the attribute.
    style = element.get("style")
    if style:
        cleaned = _STYLE_FILL_RE.sub("", style)
        cleaned = _STYLE_STROKE_ANY_RE.sub("", cleaned)
        cleaned = re.sub(r";\s*;", ";", cleaned).strip(" ;")
        if cleaned:
            element.set("style", cleaned)
        else:
            element.attrib.pop("style", None)

    element.set("fill-opacity", "1")
    element.set("opacity", "1")


_PAINTABLE_TAGS = frozenset({
    "path", "rect", "circle", "ellipse", "polygon", "polyline",
    "line", "text", "tspan", "g", "use",
})
_SKIPPED_PAINT_TAGS = frozenset({
    "defs", "clipPath", "mask", "style", "metadata", "title", "desc",
})


def _mutate_tree_in_place(root, target_hex: str, bleed_inches: float) -> None:
    """Apply per-color black/white rewrite + bleed expansion to a parsed SVG root."""
    min_x, min_y, vb_w, vb_h = _parse_viewbox(root)

    width_inches = _parse_length_to_inches(root.get("width", ""), vb_w)
    height_inches = _parse_length_to_inches(root.get("height", ""), vb_h)
    uupi_x = vb_w / width_inches if width_inches > 0 else _SVG_DEFAULT_USER_UNITS_PER_INCH
    uupi_y = vb_h / height_inches if height_inches > 0 else _SVG_DEFAULT_USER_UNITS_PER_INCH
    bleed_x = bleed_inches * uupi_x
    bleed_y = bleed_inches * uupi_y

    new_min_x = min_x - bleed_x
    new_min_y = min_y - bleed_y
    new_w = vb_w + 2 * bleed_x
    new_h = vb_h + 2 * bleed_y

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local = _strip_ns(element.tag)
        if local in _SKIPPED_PAINT_TAGS or local == "svg":
            continue
        if local in _PAINTABLE_TAGS:
            _rewrite_fill(element, target_hex)

    root.set("viewBox", f"{new_min_x} {new_min_y} {new_w} {new_h}")
    new_width_in = width_inches + 2 * bleed_inches
    new_height_in = height_inches + 2 * bleed_inches
    root.set("width", f"{new_width_in:.4f}in")
    root.set("height", f"{new_height_in:.4f}in")

    white_rect = etree.SubElement(root, f"{{{_SVG_NS}}}rect")
    white_rect.set("x", str(new_min_x))
    white_rect.set("y", str(new_min_y))
    white_rect.set("width", str(new_w))
    white_rect.set("height", str(new_h))
    white_rect.set("fill", "#FFFFFF")
    white_rect.set("stroke", "none")
    root.remove(white_rect)
    root.insert(0, white_rect)


def mutate_parsed_svg_for_color(
    parsed_root, target_hex: str, bleed_inches: float = 0.125
) -> bytes:
    """Variant of mutate_svg_for_color that takes a pre-parsed root.

    Deepcopies the input so the caller's tree is never modified. Use this when
    you're going to mutate the same SVG for many different target colors —
    avoids re-parsing the bytes N+1 times.
    """
    root_copy = copy.deepcopy(parsed_root)
    _mutate_tree_in_place(root_copy, target_hex, bleed_inches)
    return etree.tostring(root_copy, xml_declaration=True, encoding="utf-8")


def mutate_svg_for_color(
    svg_bytes: bytes, target_hex: str, bleed_inches: float = 0.125
) -> bytes:
    """Return a modified SVG where target-hex paths are black on white, with bleed.

    The SVG's viewBox is extended outward by `bleed_inches` on all sides, and
    a full-canvas white <rect> is inserted as the first child so the bleed
    region renders as white.
    """
    root = etree.fromstring(svg_bytes)
    _mutate_tree_in_place(root, target_hex, bleed_inches)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def rasterize_svg_to_pil(svg_bytes: bytes, dpi: int) -> Image.Image:
    """Render SVG bytes to a PIL Image at the given DPI via svglib+reportlab."""
    drawing = svg2rlg(io.BytesIO(svg_bytes))
    if drawing is None:
        raise RuntimeError("svglib failed to parse the SVG.")
    # renderPM returns a PIL Image when fmt='PIL' and writes to a buffer otherwise.
    # drawToString with fmt='PNG' is the most reliable path.
    png_bytes = renderPM.drawToString(drawing, fmt="PNG", dpi=dpi)
    return Image.open(io.BytesIO(png_bytes))


def render_separation_pdf(
    svg_bytes: bytes,
    target_hex: str,
    output_path: Path,
    dpi: int = 300,
    bleed_inches: float = 0.125,
) -> None:
    """Render a single grayscale black-on-white PDF for the target color."""
    mutated = mutate_svg_for_color(svg_bytes, target_hex, bleed_inches=bleed_inches)
    raster = rasterize_svg_to_pil(mutated, dpi=dpi)
    grayscale = raster.convert("L")

    # Compute physical page size in points (1pt = 1/72 inch).
    width_px, height_px = grayscale.size
    page_w_pts = (width_px / dpi) * _POINTS_PER_INCH
    page_h_pts = (height_px / dpi) * _POINTS_PER_INCH

    # Keep the raster in-memory and embed in a reportlab PDF.
    png_buffer = io.BytesIO()
    grayscale.save(png_buffer, format="PNG", optimize=True)
    png_buffer.seek(0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = rl_canvas.Canvas(str(output_path), pagesize=(page_w_pts, page_h_pts))
    canvas.drawImage(
        ImageReader(png_buffer),
        0, 0,
        width=page_w_pts,
        height=page_h_pts,
        preserveAspectRatio=False,
        mask=None,
    )
    canvas.showPage()
    canvas.save()
