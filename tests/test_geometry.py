"""Unit tests for the pure spray-reach geometry helper.

`geometry.py` is stdlib-only (no homeassistant import), so we file-load it
directly to avoid executing the package __init__.py (which imports HA).
"""
import importlib.util
import pathlib

_GEO_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "aiper_irrisense" / "geometry.py"
)
_spec = importlib.util.spec_from_file_location("geometry", _GEO_PATH)
geo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(geo)


def test_origin_is_none():
    # Head at the origin — nothing is being sprayed.
    assert geo.spray_reach_m(0, 0) is None
    assert geo.spray_reach_m(0.0, 0.0) is None


def test_known_reach_axis():
    # ~1319 units measured against a ~11 m tape reach (empirical calibration).
    assert geo.spray_reach_m(1319, 0) == 11.2
    assert geo.spray_reach_m(0, 1319) == 11.2  # radius, axis-independent


def test_known_reach_diagonal():
    # 3-4-5 triangle scaled ×100 -> radius 500 units.
    assert geo.spray_reach_m(300, 400) == 4.2


def test_negative_coordinates_use_magnitude():
    # x/y are signed ground coordinates; reach is the magnitude.
    assert geo.spray_reach_m(-300, -400) == 4.2
    assert geo.spray_reach_m(-1319, 0) == 11.2


def test_non_numeric_is_none():
    assert geo.spray_reach_m(None, 5) is None
    assert geo.spray_reach_m(5, None) is None
    assert geo.spray_reach_m("x", 5) is None
    assert geo.spray_reach_m(None, None) is None
