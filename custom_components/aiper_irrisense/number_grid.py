"""Grid helpers for the threshold numbers.

The device stores the weather-skip thresholds on a coarse grid (0.1 in of rain,
0.1 m/s of wind) - the same grid the Aiper app offers. A value entered in a
different unit rarely lands on that grid, so it is snapped before it goes to
the cloud, exactly like the app does.

Kept free of Home Assistant imports so it can be unit-tested on its own.
"""

from __future__ import annotations


def snap_to_grid(
    value: float, step: float, minimum: float, maximum: float
) -> float:
    """Snap ``value`` onto the device grid and clamp it to the device range.

    ``step``/``minimum``/``maximum`` are expressed in the device's own unit, so
    the caller converts first and snaps second. The result is rounded to three
    decimals to keep float noise (0.30000000000000004) off the wire.
    """
    snapped = round(value / step) * step
    return round(min(max(snapped, minimum), maximum), 3)
