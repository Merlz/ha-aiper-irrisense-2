"""Pure geometry helpers for the live spray target.

The device reports the live spray target as Cartesian ``x`` / ``y`` in
device-local units, with the stationary sprinkler head at the origin
(0, 0). The throw distance is therefore the Euclidean radius
``hypot(x, y)``.

The protocol carries no physical unit for x/y — the app scales them only to
fit the on-screen map, it never converts to metres. The unit below is an
empirical calibration: one device's farthest line point read ~1319 units
against a tape-measured reach of ~11 m, i.e. 1 unit ~= 1/3 inch (~8.47 mm),
consistent with the device's imperial dosing. Treat the metre value as
approximate; a second field measurement would tighten it.
"""
from __future__ import annotations

import math

# 1 device-local coordinate unit ~= 1/3 inch, empirically calibrated (~0.008467 m).
_UNIT_METRES = 25.4 / 3 / 1000


def spray_reach_m(x: object, y: object) -> float | None:
    """Throw distance (metres) of the live spray target at ``x`` / ``y``.

    Returns ``None`` for the origin (nothing sprayed) or non-numeric input.
    """
    if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
        return None
    if x == 0 and y == 0:
        return None
    return round(math.hypot(x, y) * _UNIT_METRES, 1)
