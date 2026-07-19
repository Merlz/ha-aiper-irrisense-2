"""Pure (HA-free) helpers for the Aiper weather entity. Unit-tested with stdlib pytest."""
from __future__ import annotations

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def parse_weather_payload(raw: str | dict | None) -> dict | None:
    """`/weatherkit/getWeather` returns its payload as a JSON *string* in `data`.

    Accept the string (parse it), a pre-parsed dict (passthrough), or None.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            _LOGGER.debug("weather: data not JSON: %.120s", raw)
            return None
        return obj if isinstance(obj, dict) else None
    return None


# Apple WeatherKit conditionCode -> HA condition. daylight picks sunny/clear-night.
# Covers the common set; unknown codes log once and fall back to "cloudy".
_CONDITION_MAP: dict[str, str] = {
    "PartlyCloudy": "partlycloudy",
    "MostlyCloudy": "cloudy",
    "Cloudy": "cloudy",
    "Foggy": "fog",
    "Haze": "fog",
    "Smoky": "fog",
    "Breezy": "windy",
    "Windy": "windy",
    "Drizzle": "rainy",
    "Rain": "rainy",
    "Showers": "rainy",
    "Precipitation": "rainy",
    "HeavyRain": "pouring",
    "Thunderstorms": "lightning-rainy",
    "IsolatedThunderstorms": "lightning-rainy",
    "ScatteredThunderstorms": "lightning-rainy",
    "StrongStorms": "lightning-rainy",
    "Flurries": "snowy",
    "Snow": "snowy",
    "HeavySnow": "snowy",
    "SnowShowers": "snowy",
    "BlowingSnow": "snowy",
    "Blizzard": "snowy",
    "Sleet": "snowy-rainy",
    "FreezingRain": "snowy-rainy",
    "FreezingDrizzle": "snowy-rainy",
    "WintryMix": "snowy-rainy",
    "Hail": "hail",
    "Hot": "exceptional",
    "Frigid": "exceptional",
    "Hurricane": "exceptional",
    "TropicalStorm": "exceptional",
}

_seen_unknown: set[str] = set()


def ha_condition(condition_code: str | None, daylight: bool) -> str:
    if condition_code in ("Clear", "MostlyClear"):
        return "sunny" if daylight else "clear-night"
    mapped = _CONDITION_MAP.get(condition_code or "")
    if mapped is None:
        if condition_code and condition_code not in _seen_unknown:
            _seen_unknown.add(condition_code)
            _LOGGER.warning("Unmapped WeatherKit conditionCode %r -> cloudy", condition_code)
        return "cloudy"
    return mapped


def _pct(value: Any) -> float | None:
    """WeatherKit 0-1 fraction -> HA percent."""
    if isinstance(value, (int, float)):
        return round(float(value) * 100, 1)
    return None


def current_attrs(current: dict) -> dict:
    c = current or {}
    return {
        "temperature": c.get("temperature"),
        "apparent_temperature": c.get("temperatureApparent"),
        "dew_point": c.get("temperatureDewPoint"),
        "humidity": _pct(c.get("humidity")),
        "pressure": c.get("pressure"),
        "wind_speed": c.get("windSpeed"),
        "wind_gust_speed": c.get("windGust"),
        "wind_bearing": c.get("windDirection"),
        "uv_index": c.get("uvIndex"),
        "visibility": c.get("visibility"),
        "cloud_coverage": _pct(c.get("cloudCover")),
        "condition": ha_condition(c.get("conditionCode"), bool(c.get("daylight", True))),
    }


def daily_forecast(days: list[dict]) -> list[dict]:
    out: list[dict] = []
    for d in days or []:
        if not isinstance(d, dict):
            continue
        out.append(
            {
                "datetime": d.get("forecastStart"),
                "condition": ha_condition(d.get("conditionCode"), daylight=True),
                "native_temperature": d.get("temperatureMax"),
                "native_templow": d.get("temperatureMin"),
                "precipitation_probability": _pct(d.get("precipitationChance")),
                "native_precipitation": d.get("precipitationAmount"),
                "uv_index": d.get("maxUvIndex"),
                "native_wind_speed": d.get("windSpeedMax"),
            }
        )
    return out


def resolve_coords(
    latitude: float | None, longitude: float | None
) -> tuple[float, float] | None:
    if latitude is None or longitude is None:
        return None
    if latitude == 0 and longitude == 0:
        return None
    return (float(latitude), float(longitude))
