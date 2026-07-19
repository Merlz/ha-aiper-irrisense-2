import importlib.util
import json
import pathlib

# Load weather_helpers DIRECTLY by file path. A normal
# `from custom_components.aiper_irrisense import weather_helpers` would execute
# the package __init__.py, which imports homeassistant (not installed in this
# test env). weather_helpers itself is pure stdlib, so file-loading it is clean.
_WH_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components" / "aiper_irrisense" / "weather_helpers.py"
)
_spec = importlib.util.spec_from_file_location("weather_helpers", _WH_PATH)
wh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wh)

FIX = pathlib.Path(__file__).parent / "fixtures" / "weatherkit_sample.json"
SAMPLE = FIX.read_text()


def test_parse_weather_payload_from_json_string():
    # WeatherKit returns the payload as a JSON *string* in data
    out = wh.parse_weather_payload(SAMPLE)
    assert out is not None
    assert out["currentWeather"]["temperature"] == 23.29
    assert len(out["forecastDaily"]["days"]) == 2


def test_parse_weather_payload_passthrough_dict():
    d = json.loads(SAMPLE)
    assert wh.parse_weather_payload(d) == d


def test_parse_weather_payload_garbage_returns_none():
    assert wh.parse_weather_payload("not json") is None
    assert wh.parse_weather_payload(None) is None


def test_ha_condition_known_codes():
    assert wh.ha_condition("Clear", daylight=True) == "sunny"
    assert wh.ha_condition("Clear", daylight=False) == "clear-night"
    assert wh.ha_condition("PartlyCloudy", daylight=True) == "partlycloudy"
    assert wh.ha_condition("Rain", daylight=True) == "rainy"
    assert wh.ha_condition("HeavyRain", daylight=True) == "pouring"
    assert wh.ha_condition("Thunderstorms", daylight=True) == "lightning-rainy"
    assert wh.ha_condition("Snow", daylight=True) == "snowy"


def test_ha_condition_unknown_falls_back():
    assert wh.ha_condition("SomeNewAppleCode", daylight=True) == "cloudy"
    assert wh.ha_condition(None, daylight=True) == "cloudy"


def test_current_attrs_scales_fractions():
    cur = json.loads(SAMPLE)["currentWeather"]
    a = wh.current_attrs(cur)
    assert a["temperature"] == 23.29
    assert a["humidity"] == 85.0          # 0.85 -> %
    assert a["cloud_coverage"] == 53.0    # 0.53 -> %
    assert a["wind_bearing"] == 133
    assert a["condition"] == "partlycloudy"  # PartlyCloudy -> partlycloudy


def test_daily_forecast_shape():
    days = json.loads(SAMPLE)["forecastDaily"]["days"]
    fc = wh.daily_forecast(days)
    assert len(fc) == 2
    d0 = fc[0]
    assert d0["datetime"] == "2026-06-23T22:00:00Z"
    assert d0["native_temperature"] == 24.1
    assert d0["native_templow"] == 13.2
    assert d0["precipitation_probability"] == 43.0   # 0.43 -> %
    assert d0["native_precipitation"] == 0.0
    assert fc[1]["condition"] == "rainy"
    assert fc[1]["native_precipitation"] == 3.4


def test_resolve_coords():
    assert wh.resolve_coords(53.55, 9.99) == (53.55, 9.99)
    assert wh.resolve_coords(None, 9.99) is None
    assert wh.resolve_coords(0.0, 0.0) is None   # unset HA home -> skip
