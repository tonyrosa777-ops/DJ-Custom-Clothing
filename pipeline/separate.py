"""SVG parsing + color identification.

Extracts unique fill colors from an SVG, dedupes via CIEDE2000 delta-E,
filters near-white backgrounds, preserves white-on-dark as an explicit
"White_INK" layer, and sorts by coverage area (largest first).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from lxml import etree

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
_STYLE_FILL_RE = re.compile(r"fill\s*:\s*(#[0-9a-fA-F]{3,6}|none|[a-zA-Z]+)")
_SVG_NS = "http://www.w3.org/2000/svg"


@dataclass
class ColorLayer:
    hex: str
    name: str
    path_count: int
    coverage_pct: float = 0.0
    is_white_ink: bool = False


@dataclass
class SeparationResult:
    layers: list[ColorLayer] = field(default_factory=list)
    only_one_color: bool = False
    complex_design: bool = False


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


def _iter_colored_elements(root):
    """Yield every element that has a resolvable fill color."""
    for element in root.iter():
        tag = etree.QName(element).localname if isinstance(element.tag, str) else None
        if tag in (None, "defs", "clipPath", "mask", "style", "metadata", "title", "desc"):
            continue
        fill = _extract_fill(element)
        if fill is not None:
            yield element, fill


def _detect_background(root) -> str | None:
    """Detect a whole-canvas background rect/path. Return its normalized hex or None."""
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

    # Look at the first few children of <svg> for a full-canvas rect.
    for child in root:
        tag = etree.QName(child).localname if isinstance(child.tag, str) else None
        if tag != "rect":
            continue
        try:
            w = float(child.get("width", "0"))
            h = float(child.get("height", "0"))
        except ValueError:
            continue
        if w >= vb_w * 0.98 and h >= vb_h * 0.98:
            return _extract_fill(child)
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


def _compute_coverage(svg_bytes: bytes, color_hexes: list[str]) -> dict[str, float]:
    """For each color, render a low-DPI separation raster and count black pixels.

    Returns {hex: coverage_fraction} where fraction is non-white-pixels / total.
    Import is lazy because export imports numpy/svglib which are heavy at startup.
    """
    # Lazy import to avoid circular deps and keep import surface small.
    from pipeline.export import mutate_svg_for_color, rasterize_svg_to_pil

    coverage: dict[str, float] = {}
    for hex_value in color_hexes:
        try:
            mutated = mutate_svg_for_color(svg_bytes, hex_value, bleed_inches=0.0)
            image = rasterize_svg_to_pil(mutated, dpi=_COVERAGE_DPI)
            gray = image.convert("L")
            pixels = list(gray.getdata())
            total = len(pixels) or 1
            black_count = sum(1 for p in pixels if p < 128)
            coverage[hex_value] = black_count / total
        except Exception as exc:  # pragma: no cover — coverage is best-effort
            log.warning("Coverage render failed for %s: %s", hex_value, exc)
            coverage[hex_value] = 0.0
    return coverage


def extract_colors(svg_bytes: bytes) -> SeparationResult:
    """Parse the SVG, return the color layers ready for per-color PDF generation."""
    try:
        root = etree.fromstring(svg_bytes)
    except etree.XMLSyntaxError as exc:
        raise NoColorsDetected(f"Invalid SVG payload: {exc}") from exc

    fill_counts: dict[str, int] = {}
    raw_fills: list[str] = []
    for _, fill_hex in _iter_colored_elements(root):
        fill_counts[fill_hex] = fill_counts.get(fill_hex, 0) + 1
        raw_fills.append(fill_hex)

    if not fill_counts:
        raise NoColorsDetected("No fill colors found in vectorized SVG.")

    background_hex = _detect_background(root)
    dark_background = background_hex is not None and not is_near_white(background_hex)

    # Dedupe via delta-E clustering.
    dedup_map = _dedupe_colors(list(fill_counts.keys()))
    canonical_counts: dict[str, int] = {}
    for original_hex, count in fill_counts.items():
        canonical = dedup_map[original_hex]
        canonical_counts[canonical] = canonical_counts.get(canonical, 0) + count

    # If the background color made it into our list (it typically will), drop it.
    filtered: dict[str, int] = {}
    for hex_value, count in canonical_counts.items():
        if background_hex and colors_match(hex_value, background_hex, threshold=_DEDUPE_THRESHOLD):
            continue
        if is_near_white(hex_value) and not dark_background:
            # Pure/near-white on a white (or unknown) canvas = background, skip.
            continue
        filtered[hex_value] = count

    if not filtered:
        raise NoColorsDetected("No printable colors detected — try a higher resolution image.")

    # Compute coverage for sort order.
    coverage_by_hex = _compute_coverage(svg_bytes, list(filtered.keys()))

    layers: list[ColorLayer] = []
    for hex_value, path_count in filtered.items():
        is_white_ink = dark_background and is_near_white(hex_value)
        layers.append(
            ColorLayer(
                hex=hex_value,
                name="White_INK" if is_white_ink else name_color(hex_value),
                path_count=path_count,
                coverage_pct=round(coverage_by_hex.get(hex_value, 0.0) * 100, 2),
                is_white_ink=is_white_ink,
            )
        )

    # Sort by coverage desc; path_count is the stable tiebreaker.
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
    )
