"""Unit tests for the pure schedule/plan parsing + formatting layer.

`schedule.py` is stdlib-only (no homeassistant import), so we file-load it
directly to avoid executing the package __init__.py (which imports HA).
Fixtures are real WrPlan* command payloads captured live from a device
(firmware V3.11.6) over MQTT.
"""
import importlib.util
import json
import pathlib
import sys

_SCHED_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "aiper_irrisense" / "schedule.py"
)
_spec = importlib.util.spec_from_file_location("schedule", _SCHED_PATH)
sched = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve its module via sys.modules.
sys.modules["schedule"] = sched
_spec.loader.exec_module(sched)

_FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((_FIX / f"{name}.json").read_text())


def test_parse_overview_returns_used_ids():
    used = sched.parse_overview(_load("plan_overview"))
    assert used == [1, 2]


def test_parse_plan_detail_line_zone_paused():
    p = sched.parse_plan_detail(_load("plan_detail_1"))
    assert p.plan_id == 1
    assert p.zone_name == "Line_1"
    assert p.zone_type == 1  # Line
    assert p.enabled is False
    assert p.start_time == "12:42"
    assert p.weekdays == [0, 1]
    assert p.repeat_type == 2
    assert p.depth == 0.9  # rounded from raw 0.8999997...
    assert p.estimated_time == 80


def test_parse_plan_detail_point_zone_active():
    p = sched.parse_plan_detail(_load("plan_detail_2"))
    assert p.plan_id == 2
    assert p.zone_name == "Teich"
    assert p.zone_type == 2  # Point
    assert p.enabled is True
    assert p.point_time == 150
    assert p.repeat_type == 3


def test_weekday_abbr_subset():
    assert sched.weekday_abbr([0, 1]) == "Su Mo"  # 0=Sun, 1=Mon


def test_weekday_abbr_all_seven_is_daily():
    assert sched.weekday_abbr([0, 1, 2, 3, 4, 5, 6]) == "Daily"


def test_weekday_abbr_empty():
    assert sched.weekday_abbr([]) == "—"


def test_dose_label_line_zone_is_mm():
    p = sched.parse_plan_detail(_load("plan_detail_1"))  # Line, depth 0.9"
    assert sched.dose_label(p) == "23 mm"  # 0.9 * 25.4 = 22.86 -> 23


def test_dose_label_point_zone_is_minutes():
    p = sched.parse_plan_detail(_load("plan_detail_2"))  # Point, 150 min
    assert sched.dose_label(p) == "150 min"
