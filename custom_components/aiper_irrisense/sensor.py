"""Sensor platform for the Aiper Irrisense 2.

Field names are confirmed from a live diagnostics dump:
  * equipment.name, version, wifiRssi, online
  * wr_info.mainFirmwareVersion / mcuFirmwareVersion / valveFirmwareVersion /
    bluetoothFirmwareVersion
  * stats.totalRecordCount, totalWaterYield, totalWaterSavingAmount
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = []
    for dev in coordinator.devices:
        sn = dev.get("sn")
        if not sn:
            continue
        entities.extend(
            [
                ActiveZoneSensor(coordinator, sn),
                ActiveElapsedSensor(coordinator, sn),
                ActiveTotalSensor(coordinator, sn),
                ActiveProgressSensor(coordinator, sn),
                ActivePressureSensor(coordinator, sn),
                ActiveRepairLayerSensor(coordinator, sn),
                FirmwareSensor(coordinator, sn),
                McuFirmwareSensor(coordinator, sn),
                ValveFirmwareSensor(coordinator, sn),
                WifiRssiSensor(coordinator, sn),
                TotalWaterYieldSensor(coordinator, sn),
                TotalWaterSavingSensor(coordinator, sn),
                TotalWateringEventsSensor(coordinator, sn),
                LastWateringZoneSensor(coordinator, sn),
            ]
        )
    async_add_entities(entities)


# --------------------------------------------------------------------------- #
# Active zone — sourced from MQTT workInfoReport or setWorkMode ack
# --------------------------------------------------------------------------- #


class ActiveZoneSensor(IrrisenseEntity, SensorEntity):
    """Name of the zone currently being watered, or "Idle".

    extra_state_attributes carries the status-banner payload — zone type,
    dose label, elapsed seconds, device-reported progress, start/duration
    for timer cards — so a Lovelace card can render the running-state
    banner from a single entity without templating half a dozen others.
    """

    _attr_icon = "mdi:sprinkler-variant"
    _attr_translation_key = "active_zone"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "active_zone")
        self._attr_name = "Active zone"

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.active_zone_state(self._sn)
        if state and state.get("is_running"):
            name = state.get("zone_name")
            if name:
                return name
            zid = state.get("zone_id")
            return f"Zone {zid}" if zid is not None else "Running"
        return "Idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.coordinator.active_zone_state(self._sn)
        if not state or not state.get("is_running"):
            return {"is_running": False}

        # Region-type label for the card tint / unit swap.
        rtype = state.get("region_type")
        type_label = {0: "Area", 1: "Line", 2: "Point"}.get(rtype) if rtype is not None else None

        # Surface start_time (ISO) and duration_seconds so Lovelace timer
        # cards (timer-bar-card et al.) can bind directly to attributes
        # without fighting Jinja templates. The coordinator stamps a stable
        # start_time the first frame it sees a run; duration_seconds is a
        # best-effort non-None value even if the device hasn't echoed a total.
        start_ts = state.get("start_ts")
        start_iso: str | None = None
        if isinstance(start_ts, (int, float)):
            start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()

        # Surface sprinkler-motion + pressure fields the APK's
        # realTimeProgress handler publishes. `x` / `y` are the sprinkler
        # head position in the zone's coordinate system; `repair_layer` is
        # the coverage-pass counter; `water_pressure_kpa` is the live line
        # pressure.
        return {
            "is_running": True,
            "zone_id": state.get("zone_id"),
            "zone_name": state.get("zone_name"),
            "region_type": rtype,
            "region_type_label": type_label,
            "dose_label": state.get("dose_label"),
            "water_yield": state.get("water_yield"),
            "point_time": state.get("point_time"),
            "elapsed_seconds": state.get("time_sec"),
            "progress": state.get("progress"),
            "x": state.get("x"),
            "y": state.get("y"),
            "repair_layer": state.get("repair_layer"),
            "water_pressure_kpa": state.get("water_pressure"),
            "source": state.get("source"),
            "start_time": start_iso,
            "duration_seconds": state.get("duration_seconds"),
            # timer-bar-card (rianadon) parses the `duration` attribute as
            # HH:MM:SS — an int-seconds value errors with "Could not
            # convert duration: N is not of format 0:10:00." Ship both
            # shapes: integer for TimeFlow-Card / templates, string for
            # timer-bar-card.
            "duration_hms": state.get("duration_hms"),
            # True while duration_seconds is the unconfirmed 300s
            # placeholder (haven't seen a back-solvable progress frame
            # yet). Dashboards should render "--:--" / hide the countdown
            # rather than tick down a fake 5 minutes.
            "duration_pending": state.get("duration_pending", False),
        }


# --------------------------------------------------------------------------- #
# Active-run live metrics (first-class entities so Lovelace cards auto-refresh
# on every MQTT frame — markdown/template cards subscribe to state, not to
# attribute changes, so we expose the moving values directly). Each of these
# has a state that changes every `realTimeProgress` frame, which guarantees
# the dashboard ticks in real time.
# --------------------------------------------------------------------------- #


class _ActiveMetricBase(IrrisenseEntity, SensorEntity):
    """Base for per-run live metrics. Returns None when idle."""

    _attr_entity_registry_enabled_default = True

    def _live(self) -> dict[str, Any] | None:
        state = self.coordinator.active_zone_state(self._sn)
        if state and state.get("is_running"):
            return state
        return None


class ActiveElapsedSensor(_ActiveMetricBase):
    """Elapsed seconds of the current run. None when idle."""

    _attr_icon = "mdi:timer-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "s"
    _attr_translation_key = "active_elapsed"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "active_elapsed")
        self._attr_name = "Elapsed seconds"

    @property
    def native_value(self) -> int | None:
        live = self._live()
        if not live:
            return None
        t = live.get("time_sec")
        if isinstance(t, (int, float)):
            return int(t)
        return None


class ActiveTotalSensor(_ActiveMetricBase):
    """Target duration in seconds for the current run.

    Derived from the dose: point_time × 60 for Point zones, or (for Area/Line)
    waterYield mapped through typical run-time heuristics. When the device
    reports its own ``totalTime`` field we use it verbatim.
    """

    _attr_icon = "mdi:timer-sand"
    _attr_native_unit_of_measurement = "s"
    _attr_translation_key = "active_total"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "active_total")
        self._attr_name = "Run total seconds"

    @property
    def native_value(self) -> int | None:
        live = self._live()
        if not live:
            return None
        # The coordinator computes `duration_seconds` with full fallbacks
        # (device-reported total → point_time×60 → back-solve from
        # elapsed/progress → 300s placeholder) and guarantees a non-None
        # numeric when the zone is running. This keeps timer cards happy —
        # they died on `unknown`/None durations.
        val = live.get("duration_seconds")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        return None


class ActiveProgressSensor(_ActiveMetricBase):
    """Progress as a percentage 0..100. Computed from device-reported
    progress when available, falling back to elapsed/total ratio."""

    _attr_icon = "mdi:progress-clock"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_translation_key = "active_progress"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "active_progress")
        self._attr_name = "Progress"

    @property
    def native_value(self) -> float | None:
        live = self._live()
        if not live:
            return None
        p = live.get("progress")
        if isinstance(p, (int, float)):
            # Normalise 0..1 → 0..100 without clobbering already-scaled values.
            val = p * 100.0 if 0 <= p <= 1 else float(p)
            return round(max(0.0, min(100.0, val)), 1)
        # Fallback: elapsed / total.
        t = live.get("time_sec")
        # We can't call ActiveTotalSensor from here, so replicate its compute.
        pt = live.get("point_time")
        total: float | None = None
        if isinstance(pt, (int, float)) and pt > 0:
            total = float(pt) * 60.0
        if (
            total is not None
            and isinstance(t, (int, float))
            and total > 0
        ):
            return round(max(0.0, min(100.0, (t / total) * 100.0)), 1)
        return None


class ActivePressureSensor(_ActiveMetricBase):
    """Water-line pressure in kPa during the current run.

    Confirmed from the APK's realTimeProgress handler — the wire key is
    ``waterpress`` (no underscore). The device publishes this live on
    every progress frame and it's handy for a health sub-panel.
    """

    _attr_icon = "mdi:gauge"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kPa"
    _attr_translation_key = "active_pressure"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "active_pressure")
        self._attr_name = "Water pressure"

    @property
    def native_value(self) -> float | None:
        live = self._live()
        if not live:
            return None
        p = live.get("water_pressure")
        if isinstance(p, (int, float)):
            return round(float(p), 1)
        return None


class ActiveRepairLayerSensor(_ActiveMetricBase):
    """Coverage-pass counter for the active run.

    Reads the ``repairLayer`` field from realTimeProgress frames
    (confirmed at APK `IrrisenseDeviceInfoSourceMemory.java:1819`). Area
    zones where the sprinkler head re-sweeps the target shape will see
    this integer climb over the course of a run — useful for verifying
    that the device is actually making multiple passes rather than
    idling.
    """

    _attr_icon = "mdi:repeat-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "active_repair_layer"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "active_repair_layer")
        self._attr_name = "Coverage passes"

    @property
    def native_value(self) -> int | None:
        live = self._live()
        if not live:
            return None
        v = live.get("repair_layer")
        if isinstance(v, (int, float)):
            return int(v)
        return None


# --------------------------------------------------------------------------- #
# Firmware sensors
# --------------------------------------------------------------------------- #


class _FirmwareBase(IrrisenseEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _wr_key: str = ""
    _equip_fallback_key: str = ""

    @property
    def native_value(self) -> str | None:
        wr = self._slot.get("wr_info")
        if isinstance(wr, dict):
            val = wr.get(self._wr_key)
            if isinstance(val, str) and val:
                return val
        if self._equip_fallback_key:
            val = self._device_dict.get(self._equip_fallback_key)
            if isinstance(val, str) and val:
                return val
        return None


class FirmwareSensor(_FirmwareBase):
    _wr_key = "mainFirmwareVersion"
    _equip_fallback_key = "version"
    _attr_translation_key = "firmware"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "firmware")
        self._attr_name = "Firmware version"


class McuFirmwareSensor(_FirmwareBase):
    _wr_key = "mcuFirmwareVersion"
    _attr_translation_key = "firmware_mcu"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "firmware_mcu")
        self._attr_name = "MCU firmware"


class ValveFirmwareSensor(_FirmwareBase):
    _wr_key = "valveFirmwareVersion"
    _attr_translation_key = "firmware_valve"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "firmware_valve")
        self._attr_name = "Valve firmware"


# --------------------------------------------------------------------------- #
# WiFi RSSI
# --------------------------------------------------------------------------- #


class WifiRssiSensor(IrrisenseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "wifi_rssi"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "wifi_rssi")
        self._attr_name = "WiFi signal"

    @property
    def native_value(self) -> int | None:
        val = self._device_dict.get("wifiRssi")
        if isinstance(val, (int, float)):
            return int(val)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ssid = self._device_dict.get("wifiName")
        if isinstance(ssid, str):
            return {"ssid": ssid}
        return None


# --------------------------------------------------------------------------- #
# Water totals (lifetime — only totals are exposed by the backend)
# --------------------------------------------------------------------------- #


class TotalWaterYieldSensor(IrrisenseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    # Backend reports gallons (see the inch-based depth presets in const.py);
    # declare gallons and let HA convert for metric users.
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water"
    _attr_translation_key = "total_water_yield"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "total_water_yield")
        self._attr_name = "Total water delivered"

    @property
    def native_value(self) -> float | None:
        stats = self._slot.get("stats")
        if isinstance(stats, dict):
            val = stats.get("totalWaterYield")
            if isinstance(val, (int, float)):
                return float(val)
        return None


class TotalWaterSavingSensor(IrrisenseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water-check"
    _attr_translation_key = "total_water_saving"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "total_water_saving")
        self._attr_name = "Total water saved"

    @property
    def native_value(self) -> float | None:
        stats = self._slot.get("stats")
        if isinstance(stats, dict):
            val = stats.get("totalWaterSavingAmount")
            if isinstance(val, (int, float)):
                return float(val)
        return None


class TotalWateringEventsSensor(IrrisenseEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "total_events"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "total_events")
        self._attr_name = "Watering events"

    @property
    def native_value(self) -> int | None:
        stats = self._slot.get("stats")
        if isinstance(stats, dict):
            val = stats.get("totalRecordCount")
            if isinstance(val, int):
                return val
        return None


class LastWateringZoneSensor(IrrisenseEntity, SensorEntity):
    _attr_icon = "mdi:history"
    _attr_translation_key = "last_zone"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_zone")
        self._attr_name = "Last watered zone"

    @property
    def native_value(self) -> str | None:
        history = self._slot.get("history")
        if not isinstance(history, dict):
            return None
        items = (
            history.get("list")
            or history.get("records")
            or history.get("data")
            or []
        )
        if not (isinstance(items, list) and items and isinstance(items[0], dict)):
            return None
        last = items[0]
        region_id = last.get("regionId") or last.get("region_id") or last.get("mapId")
        if region_id is not None:
            try:
                name = self.coordinator.zone_name(self._sn, int(region_id))
            except (TypeError, ValueError):
                name = None
            if name:
                return name
        return last.get("name") or last.get("regionName")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        history = self._slot.get("history")
        if not isinstance(history, dict):
            return None
        items = history.get("list") or history.get("records") or history.get("data") or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            last = items[0]
            return {
                "start_time": last.get("startTime") or last.get("start_time"),
                "duration_minutes": last.get("duration") or last.get("estimatedDuration"),
                "depth_mm": last.get("depth") or last.get("waterYield"),
            }
        return None
