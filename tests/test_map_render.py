import importlib.util
import json
import pathlib

# Load map_render DIRECTLY by file path. A normal
# `from custom_components.aiper_irrisense import map_render` would execute the
# package __init__.py, which imports homeassistant (not installed in this test
# env). map_render itself is pure stdlib, so file-loading it is clean.
_MR_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "aiper_irrisense" / "map_render.py"
)
_spec = importlib.util.spec_from_file_location("map_render", _MR_PATH)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

FIX = pathlib.Path(__file__).parent / "fixtures" / "zone_map_sample.json"
REGIONS = json.loads(FIX.read_text())["regions"]


def test_idle_render_contains_all_zone_shapes():
    svg = mr.render_zone_map(REGIONS)
    assert svg.startswith("<svg")
    assert svg.count("<polygon") == 1   # type 0 (Area)
    assert svg.count("<polyline") == 1  # type 1 (Line)
    # type 2 (Point) circle + head-origin marker
    assert svg.count("<circle") >= 2


def test_zone_names_are_xml_escaped():
    svg = mr.render_zone_map(REGIONS)
    assert "Rose &amp; bed &lt;north&gt;" in svg
    assert "bed <north>" not in svg


def test_idle_render_has_no_spray_overlay():
    svg = mr.render_zone_map(REGIONS)
    assert 'class="spray"' not in svg


def test_empty_regions_renders_placeholder():
    for empty in ([], None):
        svg = mr.render_zone_map(empty or [])
        assert "no map data" in svg
        assert svg.startswith("<svg")


def test_malformed_region_is_skipped_without_raising():
    bad = REGIONS + [
        {"id": 99, "name": "broken", "type": 0, "points": "not-a-list"},
        {"id": 98, "name": "empty", "type": 0, "points": []},
        "not-a-dict",
    ]
    svg = mr.render_zone_map(bad)
    assert svg.count("<polygon") == 1  # still only the one valid Area


def test_y_axis_is_flipped():
    # Fixture point zone sits at y=+78 (device coords, +Y up). On screen it
    # must land ABOVE the origin marker => smaller SVG y than the origin's.
    svg = mr.render_zone_map(REGIONS)
    assert 'cy="-78.0"' in svg  # emitted as flipped device coordinate


ACTIVE = {
    "is_running": True,
    "zone_id": 4,
    "progress": 0.37,   # 0..1 form
    "x": 210.0,
    "y": 300.0,
}


def test_running_render_highlights_active_zone_and_spray():
    svg = mr.render_zone_map(REGIONS, ACTIVE)
    assert 'class="spray"' in svg
    assert "37%" in svg              # 0..1 progress normalised to percent
    assert mr._ACTIVE_STROKE in svg  # active palette used


def test_running_render_flips_spray_y():
    svg = mr.render_zone_map(REGIONS, ACTIVE)
    assert 'y2="-300.0"' in svg


def test_render_key_stable_when_idle():
    k1 = mr.render_key(REGIONS, None)
    k2 = mr.render_key(REGIONS, {"is_running": False, "x": 1.0, "y": 2.0})
    assert k1 == k2


def test_render_key_ignores_sub_grid_jitter():
    a1 = dict(ACTIVE, x=210.0, y=300.0)
    a2 = dict(ACTIVE, x=214.0, y=296.0)  # < COORD_QUANT/2 movement
    assert mr.render_key(REGIONS, a1) == mr.render_key(REGIONS, a2)


def test_render_key_changes_on_zone_progress_and_big_move():
    base = mr.render_key(REGIONS, ACTIVE)
    assert mr.render_key(REGIONS, dict(ACTIVE, zone_id=3)) != base
    assert mr.render_key(REGIONS, dict(ACTIVE, progress=0.42)) != base
    assert mr.render_key(REGIONS, dict(ACTIVE, x=210.0 + 2 * mr.COORD_QUANT)) != base


def test_render_key_changes_when_map_changes():
    other = [dict(r, name=str(r.get("name")) + "!") for r in REGIONS]
    assert mr.render_key(other, None) != mr.render_key(REGIONS, None)
