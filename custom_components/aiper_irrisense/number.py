"""Number platform for the Aiper Irrisense 2.

Two groups of entities live here:

**Free dose / duration input** (`DepthNumber`, `DurationNumber`).
Complements the Dose select. The Aiper app exposes the full slider range —
depth 0.1..0.9 inch (shown as 3..23 mm) and point time 1..150 minutes — far
wider than the three presets the Dose select offers. These Number entities
let the user set any in-range value. The value is written to the same dose
selection the Dose select and Start button read, so the most recent of the
two wins.

**Weather-skip thresholds** (`RainThresholdNumber`, `WindThresholdNumber`).
Exposes the two weather-skip thresholds from the watering-setting slot as
adjustable numbers: the rain amount and the wind speed above which the device
defers a scheduled run. Both write back through ``updateWateringSetting`` (full
setting object merged by the coordinator).

The device stores these as bare floats in fixed units, regardless of the user's
locale - the Aiper app is what converts them for display:

* ``rainAmount``  -> **inches** (app gauge 0.1-1.0 in, 0.1 steps). Cross-checked
  against a real device: raw 0.5 renders as ~13 mm in a metric app.
* ``windSpeed``   -> **metres/second** (app slider 2.2-20.1 m/s, 0.1 steps).
  Cross-checked: raw 8.2 renders as ~30 km/h (8.2 * 3.6 = 29.5).

Unlike sensors, a ``number`` does not follow the unit system on its own: core
only auto-converts numbers for ``NumberDeviceClass.TEMPERATURE``, and the number
platform has no ``suggested_unit_of_measurement``. Declaring the wire unit as
the entity unit would therefore show inches and m/s to every user, metric ones
included.

So the entities publish millimetres and km/h - the units the app shows the
majority of users - and convert to the wire unit at the write boundary using the
core converters. The wire unit stays an implementation detail of the cloud
protocol. Users who prefer inches, mph or knots pick that unit per entity in the
entity settings; both device classes sit in the number platform's converter
table, so core then converts display and input for them.

Input is snapped onto the device's own grid before it is sent (see
``number_grid``), so a metric user typing 13 mm gets the 0.5 in the device
supports rather than an off-grid 0.512 in.
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    DistanceConverter,
    SpeedConverter,
)

from .const import (
    DEPTH_MM_MAX,
    DEPTH_MM_MIN,
    DOMAIN,
    POINT_TIME_MAX,
    POINT_TIME_MIN,
)
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity
from .number_grid import snap_to_grid


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[NumberEntity] = []
    for dev in coordinator.devices:
        sn = dev.get("sn")
        if not sn:
            continue
        entities.extend(
            [
                DepthNumber(coordinator, sn),
                DurationNumber(coordinator, sn),
                RainThresholdNumber(coordinator, sn),
                WindThresholdNumber(coordinator, sn),
            ]
        )
    async_add_entities(entities)


class _DoseNumberBase(IrrisenseEntity, RestoreEntity, NumberEntity):
    """Free dose/duration input that feeds the shared dose selection.

    Holds its own last-entered value (restored across restarts). Writing it
    also updates the coordinator's dose selection as a label the Start button
    parses, so the Number and the Dose select share one 'what will Start use'
    value on a last-writer-wins basis.
    """

    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str, key: str) -> None:
        super().__init__(coordinator, sn, key)
        self._value: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._value = float(last.state)
            except (TypeError, ValueError):
                self._value = None

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.coordinator.set_dose_selection(self._sn, self._label_for(value))
        self.async_write_ha_state()

    def _label_for(self, value: float) -> str:
        raise NotImplementedError


class DepthNumber(_DoseNumberBase):
    """Watering depth in mm (Area/Line zones), 3..23 mm."""

    _attr_translation_key = "watering_depth"
    _attr_icon = "mdi:water"
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
    _attr_native_min_value = float(DEPTH_MM_MIN)
    _attr_native_max_value = float(DEPTH_MM_MAX)

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "watering_depth")

    def _label_for(self, value: float) -> str:
        return f"{int(round(value))} mm"


class DurationNumber(_DoseNumberBase):
    """Watering duration in minutes (Point zones), 1..150 min."""

    _attr_translation_key = "watering_duration"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = float(POINT_TIME_MIN)
    _attr_native_max_value = float(POINT_TIME_MAX)

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "watering_duration")

    def _label_for(self, value: float) -> str:
        return f"{int(round(value))} min"


class _SettingNumber(IrrisenseEntity, NumberEntity):
    """Base for a single float key in the ``setting`` slot.

    Subclasses declare the entity unit through ``_attr_native_unit_of_measurement``
    and the device's own unit and grid through ``_wire_*``; conversion between the
    two runs through ``_converter``.
    """

    _setting_key: str = ""
    _attr_mode = NumberMode.BOX

    _converter: type[BaseUnitConverter]
    _wire_unit: str = ""
    _wire_min: float = 0.0
    _wire_max: float = 0.0
    _wire_step: float = 0.1

    @property
    def native_value(self) -> float | None:
        setting = self._slot.get("setting")
        if not isinstance(setting, dict):
            return None
        val = setting.get(self._setting_key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return round(self._from_wire(float(val)), 2)
        return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_watering_setting(
            self._sn, {self._setting_key: self._to_wire(value)}
        )

    def _from_wire(self, value: float) -> float:
        return self._converter.converter_factory(
            self._wire_unit, self._attr_native_unit_of_measurement
        )(value)

    def _to_wire(self, value: float) -> float:
        wire = self._converter.converter_factory(
            self._attr_native_unit_of_measurement, self._wire_unit
        )(value)
        return snap_to_grid(wire, self._wire_step, self._wire_min, self._wire_max)


class RainThresholdNumber(_SettingNumber):
    """Rain amount above which a scheduled run is skipped (``rainAmount``)."""

    _setting_key = "rainAmount"
    _attr_device_class = NumberDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = UnitOfPrecipitationDepth.MILLIMETERS
    # 0.1-1.0 in on the device, i.e. 2.54-25.4 mm.
    _attr_native_min_value = 2.54
    _attr_native_max_value = 25.4
    _attr_native_step = 0.1
    _attr_icon = "mdi:weather-rainy"
    _attr_translation_key = "rain_threshold"

    _converter = DistanceConverter
    _wire_unit = UnitOfPrecipitationDepth.INCHES
    _wire_min = 0.1
    _wire_max = 1.0
    _wire_step = 0.1

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "rain_threshold")


class WindThresholdNumber(_SettingNumber):
    """Wind speed above which a scheduled run is skipped (``windSpeed``)."""

    _setting_key = "windSpeed"
    _attr_device_class = NumberDeviceClass.WIND_SPEED
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
    # 2.2-20.1 m/s on the device, i.e. 7.92-72.36 km/h.
    _attr_native_min_value = 7.92
    _attr_native_max_value = 72.36
    _attr_native_step = 0.1
    _attr_icon = "mdi:weather-windy"
    _attr_translation_key = "wind_threshold"

    _converter = SpeedConverter
    _wire_unit = UnitOfSpeed.METERS_PER_SECOND
    _wire_min = 2.2
    _wire_max = 20.1
    _wire_step = 0.1

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "wind_threshold")
