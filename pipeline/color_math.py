"""CIEDE2000 color distance — hand-rolled.

Replaces the `colormath` package, which depends on the removed `numpy.asscalar`
symbol and would force us to pin numpy<2.

Reference: Sharma, Wu, Dalal (2005), "The CIEDE2000 color-difference formula:
Implementation notes, supplementary test data, and mathematical observations."
"""
from __future__ import annotations

import math

WHITE_HEX = "#FFFFFF"
_NEAR_WHITE_THRESHOLD = 10.0
# Match threshold for screen-printing separation: at ΔE 15 the rendering misses
# paths that were grouped together by the dedupe step. ΔE 25 keeps cluster
# members on the same film and tracks the dedupe threshold in separate.py.
_MATCH_THRESHOLD = 25.0

# Named color palette for coarse hex-to-name lookup.
_NAMED_COLORS: dict[str, str] = {
    "Black": "#000000",
    "White": "#FFFFFF",
    "Red": "#CC2222",
    "Green": "#229944",
    "Blue": "#2244CC",
    "Yellow": "#EECC22",
    "Cyan": "#22CCCC",
    "Magenta": "#CC22CC",
    "Orange": "#EE7722",
    "Purple": "#7722AA",
    "Pink": "#EE88BB",
    "Brown": "#885533",
    "Gray": "#888888",
    "Navy": "#112266",
    "Teal": "#227777",
    "Maroon": "#660011",
    "Olive": "#667722",
}


def normalize_hex(value: str | None) -> str | None:
    """Normalize a hex color to #RRGGBB uppercase. Return None if not a hex color."""
    if not value:
        return None
    value = value.strip()
    if value.lower() in ("none", "transparent", "inherit", "currentcolor"):
        return None
    if value.startswith("#"):
        value = value[1:]
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return "#" + value.upper()


def hex_to_rgb(hex_value: str) -> tuple[int, int, int]:
    h = hex_value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _srgb_to_linear(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    # sRGB D65 matrix
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    return x, y, z


def _xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    # Normalize by D65 reference white.
    xn, yn, zn = 0.95047, 1.0, 1.08883
    x /= xn
    y /= yn
    z /= zn

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b = 200 * (fy - fz)
    return L, a, b


def hex_to_lab(hex_value: str) -> tuple[float, float, float]:
    r, g, b = hex_to_rgb(hex_value)
    lr, lg, lb = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    x, y, z = _linear_to_xyz(lr, lg, lb)
    return _xyz_to_lab(x, y, z)


def delta_e(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000 delta-E."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2

    avg_L = (L1 + L2) / 2.0
    C1 = math.sqrt(a1 * a1 + b1 * b1)
    C2 = math.sqrt(a2 * a2 + b2 * b2)
    avg_C = (C1 + C2) / 2.0

    G = 0.5 * (1 - math.sqrt((avg_C ** 7) / (avg_C ** 7 + 25 ** 7)))
    a1p = (1 + G) * a1
    a2p = (1 + G) * a2
    C1p = math.sqrt(a1p * a1p + b1 * b1)
    C2p = math.sqrt(a2p * a2p + b2 * b2)
    avg_Cp = (C1p + C2p) / 2.0

    def _h(ap: float, bp: float) -> float:
        if ap == 0 and bp == 0:
            return 0.0
        h = math.degrees(math.atan2(bp, ap))
        return h + 360 if h < 0 else h

    h1p = _h(a1p, b1)
    h2p = _h(a2p, b2)

    if abs(h1p - h2p) > 180:
        avg_Hp = (h1p + h2p + 360) / 2.0
    else:
        avg_Hp = (h1p + h2p) / 2.0

    T = (
        1
        - 0.17 * math.cos(math.radians(avg_Hp - 30))
        + 0.24 * math.cos(math.radians(2 * avg_Hp))
        + 0.32 * math.cos(math.radians(3 * avg_Hp + 6))
        - 0.20 * math.cos(math.radians(4 * avg_Hp - 63))
    )

    diff_hp = h2p - h1p
    if abs(diff_hp) > 180:
        diff_hp += -360 if h2p > h1p else 360

    delta_Lp = L2 - L1
    delta_Cp = C2p - C1p
    delta_Hp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(diff_hp / 2))

    SL = 1 + (0.015 * (avg_L - 50) ** 2) / math.sqrt(20 + (avg_L - 50) ** 2)
    SC = 1 + 0.045 * avg_Cp
    SH = 1 + 0.015 * avg_Cp * T

    delta_theta = 30 * math.exp(-(((avg_Hp - 275) / 25) ** 2))
    RC = 2 * math.sqrt((avg_Cp ** 7) / (avg_Cp ** 7 + 25 ** 7))
    RT = -RC * math.sin(math.radians(2 * delta_theta))

    return math.sqrt(
        (delta_Lp / SL) ** 2
        + (delta_Cp / SC) ** 2
        + (delta_Hp / SH) ** 2
        + RT * (delta_Cp / SC) * (delta_Hp / SH)
    )


def delta_e_hex(hex1: str, hex2: str) -> float:
    return delta_e(hex_to_lab(hex1), hex_to_lab(hex2))


def is_near_white(hex_value: str, threshold: float = _NEAR_WHITE_THRESHOLD) -> bool:
    return delta_e_hex(hex_value, WHITE_HEX) < threshold


def colors_match(hex1: str, hex2: str, threshold: float = _MATCH_THRESHOLD) -> bool:
    return delta_e_hex(hex1, hex2) < threshold


def name_color(hex_value: str) -> str:
    """Return a common color name for the given hex via nearest CIEDE2000 match."""
    normalized = normalize_hex(hex_value)
    if normalized is None:
        return f"Color_{hex_value.lstrip('#').upper()}"
    target_lab = hex_to_lab(normalized)
    best_name = f"Color_{normalized.lstrip('#')}"
    best_distance = float("inf")
    for name, sample in _NAMED_COLORS.items():
        d = delta_e(target_lab, hex_to_lab(sample))
        if d < best_distance:
            best_distance = d
            best_name = name
    # If nothing is reasonably close, fall back to hex-based name.
    if best_distance > 30:
        return f"Color_{normalized.lstrip('#')}"
    return best_name
