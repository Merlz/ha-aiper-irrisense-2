"""Button platform for the Aiper Irrisense 2.

One Start and one Stop button per device. The buttons read the currently-
selected zone and dose from the coordinator (populated by the :mod:`select`
entities):

* **Start** — looks up the Zone select's current value, maps the Dose
  select's label back to a wire value (``waterYield`` for Area/Line or
  ``point_time`` for Point zones), and publishes ``setWorkMode status:1``.
* **Stop**  — stops whichever zone the *device* is actively watering
  (``coordinator.active_zone_state``), falling back to the
  currently-selected zone when idle so a user-initiated Stop still does
  something sensible.

Users pick zone + dose once via the select entities and hit a single Start.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, REGION_TYPE_POINT, parse_dose_label
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[ButtonEntity] = []
    for dev in coordinator.devices:
        sn = dev.get("sn")
        if not sn:
            continue
        entities.append(StartWateringButton(coordinator, sn))
        entities.append(StopWateringButton(coordinator, sn))

    if entities:
        async_add_entities(entities)


class StartWateringButton(IrrisenseEntity, ButtonEntity):
    """Start the currently-selected zone with the currently-selected dose."""

    _attr_icon = "mdi:play-circle"
    _attr_translation_key = "start_watering"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "start_watering")

    async def async_press(self) -> None:
        zone_id = self.coordinator.get_zone_selection(self._sn)
        if zone_id is None:
            _LOGGER.warning(
                "Start pressed but no zone is selected (sn=%s). "
                "Pick a zone via the Watering Zone select first.",
                self._sn,
            )
            return

        # Resolve dose label → wire value. parse_dose_label returns either
        # ("waterYield", float) or ("point_time", int). If the user never
        # touched the dose select and it's still the type default, we still
        # get a valid label from the coordinator.
        label = self.coordinator.get_dose_selection(self._sn)
        water_yield: float | None = None
        point_time: int | None = None
        if label:
            parsed = parse_dose_label(label)
            if parsed is not None:
                kind, value = parsed
                if kind == "waterYield":
                    water_yield = float(value)
                elif kind == "point_time":
                    point_time = int(value)

        # Guardrail: if the selected zone is a Point but the user's stored
        # dose label is still a mm value (or vice versa), let
        # async_start_zone fall back to the zone-map default by passing
        # both as None. This covers the race where the user picks a new
        # zone but RestoreEntity hasn't kicked in yet.
        region = self.coordinator._region_for(self._sn, zone_id)  # noqa: SLF001
        if region is not None:
            rtype = int(region.get("type", 0))
            if rtype == REGION_TYPE_POINT and point_time is None:
                water_yield = None  # don't send the wrong field
            elif rtype != REGION_TYPE_POINT and water_yield is None:
                point_time = None

        await self.coordinator.async_start_zone(
            self._sn,
            zone_id,
            water_yield=water_yield,
            point_time=point_time,
        )


class StopWateringButton(IrrisenseEntity, ButtonEntity):
    """Stop whichever zone is actively watering, or the selected zone as fallback."""

    _attr_icon = "mdi:stop-circle"
    _attr_translation_key = "stop_watering"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "stop_watering")

    async def async_press(self) -> None:
        # Prefer the device's own "what's running now" — the selection
        # may have moved to a different zone while one is still watering.
        target: int | None = None
        active = self.coordinator.active_zone_state(self._sn)
        if active and active.get("is_running"):
            target = active.get("zone_id")
        if target is None:
            target = self.coordinator.get_zone_selection(self._sn)
        if target is None:
            _LOGGER.warning(
                "Stop pressed but no zone is active or selected (sn=%s)",
                self._sn,
            )
            return
        await self.coordinator.async_stop_zone(self._sn, int(target))
