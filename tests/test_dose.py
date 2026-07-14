"""Unit tests for dose-label parsing, including free (non-preset) values.

`const.py` is stdlib-only, so we file-load it directly.
"""
import importlib.util
import pathlib

import pytest

_CONST_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "aiper_irrisense" / "const.py"
)
_spec = importlib.util.spec_from_file_location("const", _CONST_PATH)
const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(const)

MM = 25.4


def test_presets_unchanged():
    assert const.parse_dose_label("3 mm") == ("waterYield", 0.1)
    assert const.parse_dose_label("6 mm") == ("waterYield", 0.25)
    assert const.parse_dose_label("13 mm") == ("waterYield", 0.5)
    assert const.parse_dose_label("1 min") == ("point_time", 1)
    assert const.parse_dose_label("5 min") == ("point_time", 5)
    assert const.parse_dose_label("10 min") == ("point_time", 10)


def test_free_mm_maps_to_inch():
    kind, val = const.parse_dose_label("18 mm")
    assert kind == "waterYield"
    assert val == pytest.approx(18 / MM)
    kind, val = const.parse_dose_label("23 mm")
    assert kind == "waterYield"
    assert val == pytest.approx(23 / MM)


def test_free_minutes():
    assert const.parse_dose_label("120 min") == ("point_time", 120)
    assert const.parse_dose_label("150 min") == ("point_time", 150)


def test_garbage_is_none():
    assert const.parse_dose_label("abc") is None
    assert const.parse_dose_label("") is None
    assert const.parse_dose_label("mm") is None
    assert const.parse_dose_label("5 mmm") is None
    assert const.parse_dose_label("min") is None
