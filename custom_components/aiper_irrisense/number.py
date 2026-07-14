"""Number platform: free dose / duration input.

Complements the Dose select. The Aiper app exposes the full slider range —
depth 0.1..0.9 inch (shown as 3..23 mm) and point time 1..150 minutes — far
wider than the three presets the Dose select offers. These Number entities
let the user set any in-range value. The value is written to the same dose
selection the Dose select and Start button read, so the most recent of the
two wins.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DEPTH_MM_MAX,
    DEPTH_MM_MIN,
    DOMAIN,
    POINT_TIME_MAX,
    POINT_TIME_MIN,
)
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[NumberEntity] = []
    for dev in coordinator.devices:
        sn = dev.get("sn")
        if not sn:
            continue
        entities.append(DepthNumber(coordinator, sn))
        entities.append(DurationNumber(coordinator, sn))
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
        self._attr_name = "Watering depth"

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
        self._attr_name = "Watering duration"

    def _label_for(self, value: float) -> str:
        return f"{int(round(value))} min"
