"""Zone-map image platform for the Aiper Irrisense 2.

Renders the cached S3 zone map (plus live run state) as SVG. The heavy
lifting lives in map_render.py (pure, unit-tested); this module only wires
it to an ImageEntity with a throttled image_last_updated bump.
"""
from __future__ import annotations

import time

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity
from .map_render import render_key, render_zone_map

# Floor between image_last_updated bumps. realTimeProgress frames arrive
# every ~1-2 s during a run; every bump costs a recorder state row plus a
# frontend refetch, so material changes are announced at most once per
# window. A skipped change is picked up by the next frame or REST poll.
MIN_BUMP_INTERVAL = 5.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = [
        ZoneMapImage(hass, coordinator, sn)
        for dev in coordinator.devices
        if (sn := dev.get("sn"))
    ]
    async_add_entities(entities)


class ZoneMapImage(IrrisenseEntity, ImageEntity):
    """SVG map of the device's watering zones with live run overlay."""

    _attr_content_type = "image/svg+xml"
    _attr_name = "Zone map"

    def __init__(
        self, hass: HomeAssistant, coordinator: IrrisenseCoordinator, sn: str
    ) -> None:
        IrrisenseEntity.__init__(self, coordinator, sn, "zone_map")
        ImageEntity.__init__(self, hass)
        self._render_state = self._current_render_state()
        self._last_key = render_key(*self._render_state)
        self._last_bump = time.monotonic()
        self._attr_image_last_updated = dt_util.utcnow()

    def _current_render_state(self) -> tuple[list[dict], dict | None]:
        return (
            # zone_geometry_for, NOT zones_for: the slimmed regions have their
            # points[] stripped (api._parse_regions) — the renderer needs the
            # raw shapes.
            self.coordinator.zone_geometry_for(self._sn),
            self.coordinator.active_zone_state(self._sn),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self._current_render_state()
        key = render_key(*state)
        now = time.monotonic()
        if key != self._last_key and (now - self._last_bump) >= MIN_BUMP_INTERVAL:
            self._render_state = state
            self._last_key = key
            self._last_bump = now
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        # Render from the last ANNOUNCED state (not the live one) so the
        # served image always matches the image_last_updated the frontend
        # cached against.
        regions, active = self._render_state
        try:
            return render_zone_map(regions, active).encode("utf-8")
        except Exception:  # noqa: BLE001 - a broken frame must not 500 the image view
            return render_zone_map([], None).encode("utf-8")
