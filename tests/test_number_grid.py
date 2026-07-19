"""Unit tests for the pure threshold-number grid helper.

`number_grid.py` is stdlib-only (no homeassistant import), so we file-load it
directly to avoid executing the package __init__.py (which imports HA).
"""
import importlib.util
import pathlib

_GRID_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "aiper_irrisense" / "number_grid.py"
)
_spec = importlib.util.spec_from_file_location("number_grid", _GRID_PATH)
grid = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grid)

# Rain: the device stores inches on a 0.1 grid, 0.1-1.0.
RAIN = dict(step=0.1, minimum=0.1, maximum=1.0)
# Wind: metres per second on a 0.1 grid, 2.2-20.1.
WIND = dict(step=0.1, minimum=2.2, maximum=20.1)


def test_on_grid_value_survives():
    assert grid.snap_to_grid(0.5, **RAIN) == 0.5
    assert grid.snap_to_grid(8.2, **WIND) == 8.2


def test_off_grid_value_snaps_to_nearest_step():
    # 13 mm converts to 0.5118 in — the device only takes 0.5.
    assert grid.snap_to_grid(0.5118110236220472, **RAIN) == 0.5
    # 30 km/h converts to 8.333 m/s — nearest device step is 8.3.
    assert grid.snap_to_grid(8.333333333333334, **WIND) == 8.3


def test_clamped_to_device_range():
    assert grid.snap_to_grid(0.02, **RAIN) == 0.1
    assert grid.snap_to_grid(3.0, **RAIN) == 1.0
    assert grid.snap_to_grid(0.0, **WIND) == 2.2
    assert grid.snap_to_grid(99.0, **WIND) == 20.1


def test_no_float_noise_on_the_wire():
    # 0.1 * 3 is 0.30000000000000004 in binary floating point; the wire value
    # must not carry that, the cloud rejects nothing but the logs get ugly.
    assert grid.snap_to_grid(0.30000000000000004, **RAIN) == 0.3
    assert repr(grid.snap_to_grid(0.7000000000000001, **RAIN)) == "0.7"


def test_halfway_value_is_stable():
    # Python rounds halves to even; either neighbour is a valid device value,
    # what matters is that it lands on the grid.
    result = grid.snap_to_grid(0.45, **RAIN)
    assert result in (0.4, 0.5)
    assert round(result * 10) == result * 10
