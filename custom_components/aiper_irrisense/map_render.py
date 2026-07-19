"""Pure SVG renderer for the zone-map image entity.

Stdlib only — NO Home Assistant imports. Unit tests file-load this module
directly (the package __init__ pulls in homeassistant), so keep it standalone;
the region-type ints are duplicated from const.py on purpose.
"""
from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

REGION_TYPE_AREA = 0
REGION_TYPE_LINE = 1
REGION_TYPE_POINT = 2

# Quantisation grid for the live head position in render_key(); one unit is
# 1/3 inch, so 50 units ≈ 42 cm — coarse enough to suppress jitter bumps.
COORD_QUANT = 50.0

_BG = "#22303a"
_TEXT = "#eceff1"
_ZONE_STROKE = "#4dd0e1"
_ZONE_FILL = "rgba(77,208,225,0.18)"
_ACTIVE_STROKE = "#ffb74d"
_ACTIVE_FILL = "rgba(255,183,77,0.30)"
_HEAD = "#ffffff"


def _points(region: Any) -> list[tuple[float, float]]:
    """Usable (x, y) tuples of a region; [] when the shape is malformed."""
    if not isinstance(region, dict) or not isinstance(region.get("points"), list):
        return []
    out: list[tuple[float, float]] = []
    for p in region["points"]:
        if (
            isinstance(p, dict)
            and isinstance(p.get("x"), (int, float))
            and isinstance(p.get("y"), (int, float))
        ):
            out.append((float(p["x"]), float(p["y"])))
    return out


def _norm_progress(value: Any) -> int | None:
    """Device reports progress as 0..1 or 0..100 — normalize to int percent."""
    if not isinstance(value, (int, float)):
        return None
    pct = value * 100.0 if value <= 1.0 else value
    return max(0, min(100, int(pct)))


def render_key(regions: list[dict], active: dict | None) -> tuple:
    """Cheap change-detection key: only when this changes is a new image
    worth announcing (throttle gate lives in the entity)."""
    zone_id = progress = qx = qy = None
    if isinstance(active, dict) and active.get("is_running"):
        zone_id = active.get("zone_id")
        progress = _norm_progress(active.get("progress"))
        x, y = active.get("x"), active.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            qx = round(float(x) / COORD_QUANT)
            qy = round(float(y) / COORD_QUANT)
    geo = tuple(
        (r.get("id"), str(r.get("name")), r.get("type"), tuple(_points(r)))
        for r in (regions or [])
        if isinstance(r, dict)
    )
    return (geo, zone_id, progress, qx, qy)


def _placeholder() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">'
        f'<rect width="400" height="300" fill="{_BG}"/>'
        f'<text x="200" y="150" fill="{_TEXT}" font-size="24" '
        'font-family="sans-serif" text-anchor="middle" '
        'dominant-baseline="middle">no map data</text></svg>'
    )


def render_zone_map(regions: list[dict], active: dict | None = None) -> str:
    """Render the zone map as an SVG document string.

    Device coordinates are Cartesian with the sprinkler head at the origin
    and +Y pointing "up"; SVG y grows downward, so y is emitted negated.
    Never raises on malformed region data — unusable regions are skipped and
    a placeholder is returned when nothing is renderable.
    """
    shaped = [(r, _points(r)) for r in (regions or []) if isinstance(r, dict)]
    shaped = [(r, pts) for r, pts in shaped if pts]
    if not shaped:
        return _placeholder()

    xs = [x for _, pts in shaped for x, _ in pts] + [0.0]
    ys = [-y for _, pts in shaped for _, y in pts] + [0.0]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    pad = max(max_x - min_x, max_y - min_y, 100.0) * 0.12
    # Square viewBox, content centred: every device's map renders with the
    # same aspect ratio, so side-by-side dashboard cards get equal heights
    # regardless of how each garden's bounding box is shaped.
    side = max((max_x - min_x), (max_y - min_y)) + 2 * pad
    vb_x = min_x - pad - (side - ((max_x - min_x) + 2 * pad)) / 2
    vb_y = min_y - pad - (side - ((max_y - min_y) + 2 * pad)) / 2
    vb_w = vb_h = side
    scale = side
    stroke = scale * 0.006
    font = scale * 0.035
    dot = scale * 0.012

    run_state: dict | None = (
        active if isinstance(active, dict) and active.get("is_running") else None
    )
    active_id = run_state.get("zone_id") if run_state else None

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}">',
        f'<rect x="{vb_x:.1f}" y="{vb_y:.1f}" width="{vb_w:.1f}" '
        f'height="{vb_h:.1f}" fill="{_BG}"/>',
    ]

    for region, pts in shaped:
        is_active = region.get("id") == active_id and active_id is not None
        fill = _ACTIVE_FILL if is_active else _ZONE_FILL
        col = _ACTIVE_STROKE if is_active else _ZONE_STROKE
        rtype = region.get("type")
        spts = [(x, -y) for x, y in pts]
        coord_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in spts)
        if rtype == REGION_TYPE_AREA and len(spts) >= 3:
            parts.append(
                f'<polygon points="{coord_str}" fill="{fill}" '
                f'stroke="{col}" stroke-width="{stroke:.1f}"/>'
            )
        elif rtype == REGION_TYPE_LINE and len(spts) >= 2:
            parts.append(
                f'<polyline points="{coord_str}" fill="none" '
                f'stroke="{col}" stroke-width="{stroke * 2:.1f}" '
                'stroke-linecap="round"/>'
            )
        else:
            # Point zones and degenerate shapes: mark the first coordinate.
            x, y = spts[0]
            parts.append(
                f'<circle cx="{x}" cy="{y}" r="{dot * 2.5:.1f}" '
                f'fill="{fill}" stroke="{col}" stroke-width="{stroke:.1f}"/>'
            )
        lx, ly = spts[0]
        label = escape(str(region.get("name") or f"zone {region.get('id')}"))
        if is_active and run_state:
            pct = _norm_progress(run_state.get("progress"))
            if pct is not None:
                label = f"{label} · {pct}%"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly - dot * 3:.1f}" fill="{_TEXT}" '
            f'font-size="{font:.1f}" font-family="sans-serif" '
            'text-anchor="middle">' + label + "</text>"
        )

    # Sprinkler head at the device origin.
    parts.append(
        f'<circle cx="0" cy="0" r="{dot * 1.6:.1f}" fill="{_HEAD}"/>'
    )

    if run_state:
        x, y = run_state.get("x"), run_state.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            sx, sy = float(x), -float(y)
            parts.append(
                f'<g class="spray">'
                f'<line x1="0" y1="0" x2="{sx:.1f}" y2="{sy:.1f}" '
                f'stroke="{_HEAD}" stroke-width="{stroke:.1f}" '
                f'stroke-dasharray="{stroke * 3:.1f}"/>'
                f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{dot:.1f}" '
                f'fill="{_ACTIVE_STROKE}"/></g>'
            )

    parts.append("</svg>")
    return "".join(parts)
