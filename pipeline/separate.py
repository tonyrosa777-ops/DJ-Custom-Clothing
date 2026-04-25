"""SVG parsing + color identification.

Extracts unique fill colors from an SVG, dedupes via CIEDE2000 delta-E,
filters near-white backgrounds, preserves white-on-dark as an explicit
"White_INK" layer, and sorts by coverage area (largest first).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np
from lxml import etree
from svgpathtools import parse_path

from pipeline import color_math
from pipeline.color_math import (
    colors_match,
    delta_e_hex,
    is_near_white,
    name_color,
    normalize_hex,
)

log = logging.getLogger(__name__)

_DEDUPE_THRESHOLD = 10.0
_COVERAGE_DPI = 72
_BACKGROUND_COVERAGE_RATIO = 0.98  # bbox area / viewBox area required to call something "background"
_STYLE_FILL_RE = re.compile(r"fill\s*:\s*(#[0-9a-fA-F]{3,6}|none|[a-zA-Z]+)")
_STYLE_STROKE_RE = re.compile(r"stroke\s*:\s*(#[0-9a-fA-F]{3,6}|none|[a-zA-Z]+)")
_GRADIENT_REF_RE = re.compile(r"url\(\s*#[^)]+\)", re.IGNORECASE)
_SVG_NS = "http://www.w3.org/2000/svg"


@dataclass
class ColorLayer:
    hex: str
    name: str
    path_count: int
    coverage_pct: float = 0.0
    is_white_ink: bool = False
    coverage_error: bool = False
    is_stroke_only: bool = False  # color came from stroke fallback, not a fill


@dataclass
class SeparationResult:
    layers: list[ColorLayer] = field(default_factory=list)
    only_one_color: bool = False
    complex_design: bool = False
    gradients_detected: bool = False
    stroke_only_fallback: bool = False  # all colors came from strokes (no fills)


class NoColorsDetected(Exception):
    """Raised when the SVG has 0 non-background printable colors."""


def _extract_fill(element) -> str | None:
    """Return a normalized #RRGGBB from an element's fill attribute or inline style."""
    # Direct fill attribute wins.
    fill_attr = element.get("fill")
    normalized = normalize_hex(fill_attr) if fill_attr else None
    if normalized:
        return normalized

    # Fall back to style="fill:#xxx".
    style = element.get("style")
    if style:
        match = _STYLE_FILL_RE.search(style)
        if match:
            return normalize_hex(match.group(1))

    return None


def _extract_stroke(element) -> str | None:
    """Return a normalized #RRGGBB from an element's stroke attribute or inline style."""
    stroke_attr = element.get("stroke")
    normalized = normalize_hex(stroke_attr) if stroke_attr else None
    if normalized:
        return normalized

    style = element.get("style")
    if style:
        match = _STYLE_STROKE_RE.search(style)
        if match:
            return normalize_hex(match.group(1))

    return None


def _has_gradient_paint(element) -> bool:
    """True if the element references a gradient via fill/stroke url(#...)."""
    for attr in ("fill", "stroke"):
        value = element.get(attr) or ""
        if _GRADIENT_REF_RE.search(value):
            return True
    style = element.get("style") or ""
    if _GRADIENT_REF_RE.search(style):
        return True
    return False


_SKIPPED_TAGS = frozenset({"defs", "clipPath", "mask", "style", "metadata", "title", "desc"})


def _iter_paintable_elements(root):
    """Yield every element that could contribute a fill or stroke color."""
    for element in root.iter():
        tag = etree.QName(element).localname if isinstance(element.tag, str) else None
        if tag is None or tag in _SKIPPED_TAGS:
            continue
        yield element, tag


def _bbox_for_element(element) -> tuple[float, float, float, float] | None:
    """Return (x, y, w, h) bbox in user units for rect/path/polygon. None if unparseable."""
    if not isinstance(element.tag, str):
        return None
    tag = etree.QName(element).localname

    if tag == "rect":
        try:
            x = float(element.get("x", "0"))
            y = float(element.get("y", "0"))
            w = float(element.get("width", "0"))
            h = float(element.get("height", "0"))
        except ValueError:
            return None
        return x, y, w, h

    if tag == "path":
        d = element.get("d")
        if not d:
            return None
        try:
            path_obj = parse_path(d)
            if not path_obj or len(path_obj) == 0:
                return None
            xmin, xmax, ymin, ymax = path_obj.bbox()
        except Exception:
            return None
        return xmin, ymin, xmax - xmin, ymax - ymin

    if tag in ("polygon", "polyline"):
        points = element.get("points", "")
        coords = [float(n) for n in re.findall(r"[-+]?\d*\.?\d+", points)]
        if len(coords) < 4:
            return None
        xs = coords[0::2]
        ys = coords[1::2]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

    return None


def _detect_background(root) -> str | None:
    """Detect a whole-canvas background — the bottom-most rendered shape that
    fills ≥98% of the viewBox by bounding-box area.

    Vectorizer.ai sometimes emits backgrounds as <rect>, sometimes as <path>
    (typically the first filled element in document order, possibly nested in
    a <g>). We walk the tree in document order and return the first filled
    element whose bbox spans the full canvas.
    """
    viewbox = root.get("viewBox")
    if not viewbox:
        return None
    parts = viewbox.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        _, _, vb_w, vb_h = (float(p) for p in parts)
    except ValueError:
        return None
    if vb_w <= 0 or vb_h <= 0:
        return None
    viewbox_area = vb_w * vb_h

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = etree.QName(element).localname
        if tag not in ("rect", "path", "polygon"):
            continue
        fill = _extract_fill(element)
        if fill is None:
            continue
        bbox = _bbox_for_element(element)
        if bbox is None:
            continue
        _, _, bw, bh = bbox
        if bw <= 0 or bh <= 0:
            continue
        if (bw * bh) >= _BACKGROUND_COVERAGE_RATIO * viewbox_area:
            log.info("Background detected: %s (%s, bbox=%.1fx%.1f vb=%.1fx%.1f)",
                     fill, tag, bw, bh, vb_w, vb_h)
            return fill
    return None


def _dedupe_colors(hex_list: list[str]) -> dict[str, str]:
    """Return mapping from every input hex to its canonical (deduped) hex.

    Canonical = the first hex seen whose cluster this one joins.
    """
    canonical_list: list[str] = []
    mapping: dict[str, str] = {}
    for candidate in hex_list:
        if candidate in mapping:
            continue
        match = None
        for existing in canonical_list:
            if delta_e_hex(candidate, existing) < _DEDUPE_THRESHOLD:
                match = existing
                break
        if match is None:
            canonical_list.append(candidate)
            mapping[candidate] = candidate
        else:
            mapping[candidate] = match
    return mapping


# Sentinel used by _compute_coverage to flag a render that crashed.
COVERAGE_ERROR_SENTINEL: float = -1.0


def _compute_coverage(parsed_root, color_hexes: list[str]) -> dict[str, float]:
    """For each color, render a low-DPI separation raster and count black pixels.

    Takes a pre-parsed SVG root (not bytes) so we don't re-parse N+1 times.
    Returns {hex: fraction_in_[0,1]} or COVERAGE_ERROR_SENTINEL on render failure.
    Import is lazy because export imports numpy/svglib which are heavy at startup.
    """
    from pipeline.export import mutate_parsed_svg_for_color, rasterize_svg_to_pil

    coverage: dict[str, float] = {}
    for hex_value in color_hexes:
        try:
            mutated = mutate_parsed_svg_for_color(parsed_root, hex_value, bleed_inches=0.0)
            image = rasterize_svg_to_pil(mutated, dpi=_COVERAGE_DPI)
            gray = image.convert("L")
            arr = np.asarray(gray)
            total = arr.size or 1
            black_count = int(np.count_nonzero(arr < 128))
            coverage[hex_value] = black_count / total
        except Exception as exc:
            log.warning("Coverage render failed for %s: %s", hex_value, exc)
            coverage[hex_value] = COVERAGE_ERROR_SENTINEL
    return coverage


def _walk_colors(root) -> tuple[dict[str, int], dict[str, int], bool]:
    """Walk the SVG once and collect fill/stroke counts plus gradient flag."""
    fill_counts: dict[str, int] = {}
    stroke_counts: dict[str, int] = {}
    gradients_detected = False

    for element, _tag in _iter_paintable_elements(root):
        if _has_gradient_paint(element):
            gradients_detected = True

        fill = _extract_fill(element)
        if fill is not None:
            fill_counts[fill] = fill_counts.get(fill, 0) + 1

        stroke = _extract_stroke(element)
        if stroke is not None:
            stroke_counts[stroke] = stroke_counts.get(stroke, 0) + 1

    return fill_counts, stroke_counts, gradients_detected


def _filter_color_pool(
    counts: dict[str, int],
    background_hex: str | None,
    dark_background: bool,
) -> dict[str, int]:
    """Apply background + near-white filtering to a color → count map."""
    if not counts:
        return {}
    dedup_map = _dedupe_colors(list(counts.keys()))
    canonical: dict[str, int] = {}
    for original_hex, count in counts.items():
        key = dedup_map[original_hex]
        canonical[key] = canonical.get(key, 0) + count

    filtered: dict[str, int] = {}
    for hex_value, count in canonical.items():
        if background_hex and colors_match(hex_value, background_hex, threshold=_DEDUPE_THRESHOLD):
            continue
        if is_near_white(hex_value) and not dark_background:
            continue
        filtered[hex_value] = count
    return filtered


def extract_colors(svg_bytes: bytes) -> SeparationResult:
    """Parse the SVG, return the color layers ready for per-color PDF generation."""
    try:
        root = etree.fromstring(svg_bytes)
    except etree.XMLSyntaxError as exc:
        raise NoColorsDetected(f"Invalid SVG payload: {exc}") from exc

    fill_counts, stroke_counts, gradients_detected = _walk_colors(root)

    if not fill_counts and not stroke_counts:
        raise NoColorsDetected("No fill or stroke colors found in vectorized SVG.")

    background_hex = _detect_background(root)
    dark_background = background_hex is not None and not is_near_white(background_hex)

    # Try fills first.
    filtered_fills = _filter_color_pool(fill_counts, background_hex, dark_background)
    stroke_only_fallback = False
    color_pool: dict[str, int]

    if filtered_fills:
        color_pool = filtered_fills
    else:
        # Fall back to strokes if fills produced nothing usable.
        filtered_strokes = _filter_color_pool(stroke_counts, background_hex, dark_background)
        if not filtered_strokes:
            raise NoColorsDetected("No printable colors detected — try a higher resolution image.")
        color_pool = filtered_strokes
        stroke_only_fallback = True
        log.info("Falling back to stroke colors (no fills detected after filtering).")

    # Coverage computation reuses the parsed root — no re-parsing N+1 times.
    coverage_by_hex = _compute_coverage(root, list(color_pool.keys()))

    layers: list[ColorLayer] = []
    for hex_value, path_count in color_pool.items():
        is_white_ink = dark_background and is_near_white(hex_value)
        raw_coverage = coverage_by_hex.get(hex_value, COVERAGE_ERROR_SENTINEL)
        if raw_coverage == COVERAGE_ERROR_SENTINEL:
            coverage_pct = 0.0
            coverage_error = True
        else:
            coverage_pct = round(raw_coverage * 100, 2)
            coverage_error = False
        layers.append(
            ColorLayer(
                hex=hex_value,
                name="White_INK" if is_white_ink else name_color(hex_value),
                path_count=path_count,
                coverage_pct=coverage_pct,
                is_white_ink=is_white_ink,
                coverage_error=coverage_error,
                is_stroke_only=stroke_only_fallback,
            )
        )

    # Sort by coverage desc; path_count is stable tiebreaker. Errored coverages
    # sink to the bottom (0.0).
    layers.sort(key=lambda layer: (layer.coverage_pct, layer.path_count), reverse=True)

    # De-duplicate color NAMES so multiple "Red" layers become Red, Red_2, Red_3, etc.
    name_counts: dict[str, int] = {}
    for layer in layers:
        base = layer.name
        name_counts[base] = name_counts.get(base, 0) + 1
        if name_counts[base] > 1:
            layer.name = f"{base}_{name_counts[base]}"

    return SeparationResult(
        layers=layers,
        only_one_color=len(layers) == 1,
        complex_design=len(layers) > 10,
        gradients_detected=gradients_detected,
        stroke_only_fallback=stroke_only_fallback,
    )
