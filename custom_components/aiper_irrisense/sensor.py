"""Sensor platform for the Aiper Irrisense 2.

Field names are confirmed from a live diagnostics dump:
  * equipment.name, version, wifiRssi, online
  * wr_info.mainFirmwareVersion / mcuFirmwareVersion / valveFirmwareVersion /
    bluetoothFirmwareVersion
  * stats.totalRecordCount, totalWaterYield, totalWaterSavingAmount
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfLength, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_EXPERIMENTAL_SENSORS, DOMAIN
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity
from .geometry import spray_reach_m
from .schedule import dose_label, parse_plan_detail, weekday_abbr


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
                CurrentDoseSensor(coordinator, sn),
                ActiveElapsedSensor(coordinator, sn),
                ActiveTotalSensor(coordinator, sn),
                ActiveProgressSensor(coordinator, sn),
                ActiveRepairLayerSensor(coordinator, sn),
                RemainingTimeSensor(coordinator, sn),
                HeadAngleSensor(coordinator, sn),
                SprayDistanceSensor(coordinator, sn),
                FirmwareSensor(coordinator, sn),
                McuFirmwareSensor(coordinator, sn),
                ValveFirmwareSensor(coordinator, sn),
                WifiRssiSensor(coordinator, sn),
                TotalWaterYieldSensor(coordinator, sn),
                TotalWaterSavingSensor(coordinator, sn),
                TotalWateringEventsSensor(coordinator, sn),
                LastWateringZoneSensor(coordinator, sn),
                LastRunWaterSensor(coordinator, sn),
                LastRunSavingSensor(coordinator, sn),
                LastRunDurationSensor(coordinator, sn),
                LastRunStatusSensor(coordinator, sn),
                SchedulesSensor(coordinator, sn),
            ]
        )
        if entry.options.get(CONF_ENABLE_EXPERIMENTAL_SENSORS, False):
            entities.extend(
                [
                    SkipHistorySensor(coordinator, sn),
                    PesticideUsageSensor(coordinator, sn),
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

        # Surface sprinkler-motion fields the APK's realTimeProgress
        # handler publishes. `x` / `y` are the sprinkler head position
        # in the zone's coordinate system; `repair_layer` is the
        # coverage-pass counter.
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


class CurrentDoseSensor(IrrisenseEntity, SensorEntity):
    """The dose / duration the next Start will use.

    Mirrors the shared dose selection last written by the Dose select or the
    depth / duration Number entities, as a label like "13 mm" or "120 min".
    """

    _attr_icon = "mdi:water-percent"
    _attr_translation_key = "current_dose"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "current_dose")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.get_dose_selection(self._sn)


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

    @property
    def native_value(self) -> int | None:
        live = self._live()
        if not live:
            return None
        v = live.get("repair_layer")
        if isinstance(v, (int, float)):
            return int(v)
        return None


class RemainingTimeSensor(_ActiveMetricBase):
    """Estimated seconds until the current run finishes (``total − elapsed``).

    Reported only once the coordinator has back-solved a real run duration
    (``duration_pending`` cleared). While the duration is still the 300s
    placeholder we return None, so a dashboard shows "--:--" instead of
    counting down a fake five minutes.
    """

    _attr_icon = "mdi:timer-sand-complete"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "s"
    _attr_translation_key = "remaining_time"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "remaining_time")

    @property
    def native_value(self) -> int | None:
        live = self._live()
        if not live or live.get("duration_pending"):
            return None
        total = live.get("duration_seconds")
        elapsed = live.get("time_sec")
        if (
            isinstance(total, (int, float))
            and isinstance(elapsed, (int, float))
            and total > 0
        ):
            return int(max(0, total - elapsed))
        return None


class HeadAngleSensor(_ActiveMetricBase):
    """Rotation angle of the sprinkler head, 0..360°.

    The realTimeProgress stream reports the live spray target as Cartesian
    ``x`` / ``y`` (head at the origin) but no explicit head angle. The angle
    is recovered as ``(90 − atan2(y, x)) mod 360`` — the same relation the
    static map's ``rotate`` field (centi-degrees) follows, verified across
    ~50 map points on two devices.

    Device-relative: 0° is the device's own +Y reference, **not** a compass
    bearing — the data carries no geographic-North / real-world orientation.
    """

    _attr_icon = "mdi:angle-acute"
    _attr_native_unit_of_measurement = "°"
    _attr_translation_key = "head_angle"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "head_angle")

    @property
    def native_value(self) -> float | None:
        live = self._live()
        if not live:
            return None
        x = live.get("x")
        y = live.get("y")
        if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
            return None
        if x == 0 and y == 0:
            return None
        angle = (90.0 - math.degrees(math.atan2(y, x))) % 360.0
        return round(angle, 1)


class SprayDistanceSensor(_ActiveMetricBase):
    """Live throw distance of the water, in metres.

    The realTimeProgress stream reports the spray target as Cartesian
    ``x`` / ``y`` (head at the origin); the reach is the radius
    ``hypot(x, y)`` scaled to metres. The protocol carries no unit, so the
    metre value uses an empirical calibration (see ``geometry.py``) and is
    approximate. None when idle.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sprinkler-variant"
    _attr_translation_key = "spray_distance"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "spray_distance")

    @property
    def native_value(self) -> float | None:
        live = self._live()
        if not live:
            return None
        return spray_reach_m(live.get("x"), live.get("y"))


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


class McuFirmwareSensor(_FirmwareBase):
    _wr_key = "mcuFirmwareVersion"
    _attr_translation_key = "firmware_mcu"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "firmware_mcu")


class ValveFirmwareSensor(_FirmwareBase):
    _wr_key = "valveFirmwareVersion"
    _attr_translation_key = "firmware_valve"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "firmware_valve")


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

    @property
    def native_value(self) -> str | None:
        last = self._latest_history_record
        if last is None:
            return None
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
        last = self._latest_history_record
        if last is None:
            return None
        return {
            "start_time": last.get("startTime") or last.get("start_time"),
            "duration_minutes": last.get("duration") or last.get("estimatedDuration"),
            "depth_mm": last.get("depth") or last.get("waterYield"),
        }


class LastRunWaterSensor(IrrisenseEntity, SensorEntity):
    """Water delivered during the most recent completed run.

    Read from the newest watering-history record (``newWaterYield``). Unlike the
    lifetime total this is per-run, so it can drive per-run notifications or a
    comparison against a physical water meter. The unit follows the lifetime
    totals' convention (backend reports gallons; HA converts for metric users).

    ``newWaterYield`` is the water the app itself shows per run; the sibling
    ``usedVolume`` field is the potion/pesticide amount, not water, and reads 0
    without a cartridge.
    """

    # No state_class: per-run snapshot, not a measurement stream — HA rejects
    # `measurement` on device_class `water` (expects total/total_increasing).
    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water-sync"
    _attr_translation_key = "last_run_water"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_run_water")

    @property
    def native_value(self) -> float | None:
        last = self._latest_history_record
        if last is None:
            return None
        val = last.get("newWaterYield")
        if isinstance(val, (int, float)):
            return float(val)
        return None


# Outcome of a finished run, as coded on the watering-history record's
# ``taskStatus`` field. Anything outside this table renders as ``un_completed``.
TASK_STATUS_LABELS: dict[int, str] = {
    1: "completed",
    2: "fault",
    3: "weather_wind",
    4: "weather_rain",
    5: "on_rain",
    6: "overlap",
    7: "manual_stop",
    8: "water_shortage",
    9: "manual_task",
    10: "conflict",
}
_TASK_STATUS_FALLBACK = "un_completed"


class LastRunSavingSensor(IrrisenseEntity, SensorEntity):
    """Water saved during the most recent run (``waterSavingAmount``).

    Per-run counterpart to the lifetime ``total_water_saving`` total, read from
    the newest history record. Same unit convention as the other water totals
    (backend reports gallons; HA converts for metric users).
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water-check"
    _attr_translation_key = "last_run_saving"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_run_saving")

    @property
    def native_value(self) -> float | None:
        last = self._latest_history_record
        if last is None:
            return None
        val = last.get("waterSavingAmount")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
        return None


class LastRunDurationSensor(IrrisenseEntity, SensorEntity):
    """Actual run time of the most recent run (``runTime``, minutes).

    Verified against the record's planned ``duration`` on point zones: a
    10-minute dose reports ``runTime`` 11, a 1-minute dose reports 2 — i.e.
    the planned minutes plus ~1 of overhead. Line zones carry no planned
    ``duration`` but the same ``runTime`` (minutes) still applies.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:timer-outline"
    _attr_translation_key = "last_run_duration"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_run_duration")

    @property
    def native_value(self) -> int | None:
        last = self._latest_history_record
        if last is None:
            return None
        val = last.get("runTime")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
        return None


class LastRunStatusSensor(IrrisenseEntity, SensorEntity):
    """Outcome label of the most recent watering-history record (``taskStatus``).

    Surfaces why the last run ended — ``completed``, ``manual_stop``, a
    weather skip, or ``fault`` — so the reason is available to automations
    without polling the app.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(TASK_STATUS_LABELS.values()) + [_TASK_STATUS_FALLBACK]
    _attr_icon = "mdi:clipboard-check-outline"
    _attr_translation_key = "last_run_status"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_run_status")

    @property
    def native_value(self) -> str | None:
        last = self._latest_history_record
        if last is None:
            return None
        val = last.get("taskStatus")
        if not isinstance(val, int) or isinstance(val, bool):
            return None
        return TASK_STATUS_LABELS.get(val, _TASK_STATUS_FALLBACK)
# --------------------------------------------------------------------------- #
# Experimental (opt-in) — pesticide usage + skip history. Both come back empty
# on devices with no cartridge bound / no skipped runs, so the sensors report
# None until real data appears. Payload shapes are best-effort (dug
# defensively) since they can't be exercised on a non-sprayer test device.
# --------------------------------------------------------------------------- #

# Known skipType reasons; unmapped ints fall back to "type_<n>".
SKIP_TYPE_LABELS: dict[int, str] = {
    3: "weather_wind",
    4: "weather_rain",
    5: "on_rain",
    9: "manual_task",
}


def _records(payload: Any) -> list[dict[str, Any]]:
    """Records list from a `_wr` result. `_wr` already unwraps the response
    `data`, so this is either a bare list or a dict wrapping one under
    list/records/data."""
    if isinstance(payload, dict):
        payload = payload.get("list") or payload.get("records") or payload.get("data")
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


class SkipHistorySensor(IrrisenseEntity, SensorEntity):
    """Reason the most recent scheduled run was skipped (rain, wind, etc.)."""

    _attr_icon = "mdi:calendar-remove"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_skip"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_skip")
        self._attr_name = "Last skip reason"

    def _latest(self) -> dict[str, Any] | None:
        records = _records(self._slot.get("skip"))
        if not records:
            return None
        return max(records, key=lambda r: r.get("skipTaskUtcTimestampSecond") or 0)

    @property
    def native_value(self) -> str | None:
        latest = self._latest()
        if latest is None:
            return None
        stype = latest.get("skipType")
        if not isinstance(stype, int):
            return None
        return SKIP_TYPE_LABELS.get(stype, f"type_{stype}")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        records = _records(self._slot.get("skip"))
        if not records:
            return None
        latest = max(records, key=lambda r: r.get("skipTaskUtcTimestampSecond") or 0)
        ts = latest.get("skipTaskUtcTimestampSecond")
        skipped_at = None
        if isinstance(ts, int):
            skipped_at = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        return {
            "skipped_at": skipped_at,
            "skip_type": latest.get("skipType"),
            "task_id": latest.get("taskId"),
            "plan_id": latest.get("planId"),
            "count": len(records),
        }


class PesticideUsageSensor(IrrisenseEntity, SensorEntity):
    """Number of zones with logged pesticide usage; the per-zone payload is
    carried in the attributes (IrriSense 2 sprayer module)."""

    _attr_icon = "mdi:spray"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "pesticide_usage"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "pesticide_usage")
        self._attr_name = "Pesticide usage zones"

    def _usage_list(self) -> list[dict[str, Any]] | None:
        # `_wr` already unwraps the response `data`, so slot["pesticide"] is
        # the MapPesticideUsage object itself (mapId + mapPesticideUsageList).
        payload = self._slot.get("pesticide")
        if not isinstance(payload, dict):
            return None
        regions = payload.get("mapPesticideUsageList")
        if isinstance(regions, list):
            return [r for r in regions if isinstance(r, dict)]
        return None

    @property
    def native_value(self) -> int | None:
        regions = self._usage_list()
        return None if regions is None else len(regions)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        regions = self._usage_list()
        if regions is None:
            return None
        return {"regions": regions[:20]}
class SchedulesSensor(IrrisenseEntity, SensorEntity):
    """Read-only view of the device-resident watering plans (schedules).

    State is the number of plan slots in use (from ``WrPlanOverview``);
    the ``schedules`` attribute lists each plan's zone/dose/days/time and
    whether it is enabled (from ``WrPlanDetail``). Read-only: plans are
    created and edited in the Aiper app, not here.
    """

    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "schedules"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "schedules")

    @property
    def native_value(self) -> int:
        return len(self._slot.get("plan_ids", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plans_raw = self._slot.get("plans", {})
        schedules = []
        for pid in sorted(plans_raw):
            try:
                p = parse_plan_detail(plans_raw[pid])
            except (KeyError, ValueError, TypeError):
                continue
            schedules.append(
                {
                    "plan_id": p.plan_id,
                    "zone": p.zone_name,
                    "dose": dose_label(p),
                    "start_time": p.start_time,
                    "weekdays": weekday_abbr(p.weekdays),
                    "weekdays_raw": p.weekdays,
                    "repeat_type": p.repeat_type,
                    "enabled": p.enabled,
                    "estimated_minutes": p.estimated_time,
                }
            )
        return {"schedules": schedules}
