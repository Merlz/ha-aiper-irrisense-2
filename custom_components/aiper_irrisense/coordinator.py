"""Data update coordinator for the Aiper Irrisense 2 integration.

Design:

* Fast path: MQTT subscriptions deliver near-realtime state changes
  (shadow NetStat + the custom `aiper/.../upChan` and `.../WR/cloud/report`
  topics). When we receive a message, we merge it into the coordinator data
  and call `async_set_updated_data` to push entities.
* Slow path: REST polling at `DEFAULT_SCAN_INTERVAL` for `wr/getEquipmentInfo`
  (and any "is this zone running right now" evidence there).
* Cached slices (refreshed on their own cadence):
    - Zone map (S3 JSON)           → every `map_refresh_hours`
    - Watering history + stats     → every `history_refresh_hours`
    - Reminder + nozzle settings   → every `reminder_refresh_hours`
    - Watering setting + task list → every 30 min (they change more often)
* On any `workInfoReport` / `setWorkMode` ACK over MQTT, we open a 60-second
  "fast poll" window at 5s intervals so the UI catches up quickly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IrrisenseApi
from .const import (
    CONF_HISTORY_REFRESH_HOURS,
    CONF_MAP_REFRESH_HOURS,
    CONF_POLL_INTERVAL,
    CONF_REMINDER_REFRESH_HOURS,
    DEFAULT_FAST_SCAN_INTERVAL,
    DEFAULT_FAST_WINDOW_SECONDS,
    DEFAULT_HISTORY_REFRESH_HOURS,
    DEFAULT_MAP_REFRESH_HOURS,
    DEFAULT_REMINDER_REFRESH_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    POINT_TIME_LOW,
    POINT_TIME_PRESETS,
    REGION_TYPE_POINT,
    WATER_YIELD_LOW,
    WATER_YIELD_PRESETS,
    default_dose_label_for_region_type,
    label_for_point_time,
    label_for_water_yield,
    parse_dose_label,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

SIGNAL_MAP_UPDATED = f"{DOMAIN}_map_updated"
# Fired when the zone-select's current value changes, so the dose-select can
# re-render its options + label in lockstep.
SIGNAL_SELECTION_CHANGED = f"{DOMAIN}_selection_changed"

_LOGGER = logging.getLogger(__name__)

_SETTINGS_REFRESH_SECONDS = 30 * 60  # 30 min


def _extract_map_id(body: dict[str, Any] | None) -> int | None:
    """Return the zone id from an MQTT body, handling BOTH wire shapes.

    Confirmed from the Aiper APK (`IrrisenseDeviceInfoSourceMemory.java`,
    decompiled):

    * ``setWorkMode`` echoes put the zone id at top level as ``map_id``
      (small int 1..N matching ``regions[].id`` from the S3 zone-map JSON).
    * ``realTimeProgress`` / ``realTimeProgressReport`` frames — the ONLY
      frames that carry ``progress`` and ``time`` — nest the zone id inside
      ``map_info`` as ``{"id": <int>, "name": <str>, "type": <int>}``.
    * ``workInfo`` / ``workInfoReport`` frames don't carry a zone id at all
      (they only have ``status`` and ``mode``) — those return None here,
      which is fine: they're only useful for "is the device working".

    Also tolerates the legacy camelCase ``mapId`` and the older
    ``region_id`` spelling, for robustness against firmware variance.
    """
    if not isinstance(body, dict):
        return None
    # Top-level keys (setWorkMode echo, older variants)
    for key in ("map_id", "mapId", "region_id"):
        raw = body.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    # Nested under map_info (realTimeProgress frames)
    info = body.get("map_info")
    if isinstance(info, dict):
        raw = info.get("id")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return None


def _snap_to_preset(value, presets, label: str):
    """Return the nearest value in ``presets`` to ``value``.

    Logs a WARNING when the input was off-preset so automations / services
    calling with arbitrary numbers get visible feedback. Firmware silently
    drops off-preset setWorkMode frames, so this snap is what makes
    user-configurable Number entities actually work.
    """
    if value in presets:
        return value
    snapped = min(presets, key=lambda p: abs(p - value))
    _LOGGER.warning(
        "%s=%s is off-preset; snapping to %s (valid presets: %s). "
        "The firmware silently discards off-preset values.",
        label, value, snapped, list(presets),
    )
    return snapped


class IrrisenseCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinate REST + MQTT data for all Irrisense devices on one account."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: IrrisenseApi,
        entry: ConfigEntry,
    ) -> None:
        self.api = api
        self.entry = entry

        poll_s = int(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self._base_interval = timedelta(seconds=poll_s)
        self._fast_interval = timedelta(seconds=DEFAULT_FAST_SCAN_INTERVAL)
        self._fast_until: float = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=self._base_interval,
        )

        # Per-SN structure: {sn: {"equipment": ..., "wr_info": ...,
        #   "map": ..., "tasks": ..., "setting": ..., "nozzle": ...,
        #   "reminder": ..., "history": ..., "stats": ...,
        #   "mqtt": {...latest parsed topic payloads keyed by topic suffix...}}}
        self._data: dict[str, dict[str, Any]] = {}

        # Cache freshness timestamps (per SN, per slice)
        self._last_map_fetch: dict[str, float] = {}
        self._last_history_fetch: dict[str, float] = {}
        self._last_reminder_fetch: dict[str, float] = {}
        self._last_settings_fetch: dict[str, float] = {}

        # User's current Dashboard selection (controls card).
        # Populated by the ZoneSelect / DoseSelect entities; read by the
        # Start button and by the status banner. Kept here (not on the
        # entities) so restarts + entity replacement don't drop the state.
        self._zone_selection: dict[str, int] = {}    # sn → zone_id
        self._dose_selection: dict[str, str] = {}    # sn → human label ("3 mm", "5 min", ...)

        # Per-run anchoring so Lovelace timer cards (timer-bar-card, etc.)
        # can render a smooth, monotonic progress bar without drifting
        # whenever the device reports a new `elapsed` value. We stamp a
        # start_time at the moment the run is first seen for a given zone,
        # then clear it when the zone stops or changes.
        self._run_start_ts: dict[str, float] = {}   # sn → epoch seconds
        self._run_start_zone: dict[str, int] = {}   # sn → zone_id the stamp belongs to
        # Latch the first "good" duration we compute for a run so the
        # timer bar doesn't jump on noisy `progress` frames. Area zones have
        # been observed reporting progress oscillating wildly within a few
        # seconds (0% → 100% → 2% → 6%), which made back-solved duration
        # rocket between 60s and 5940s and the bar re-scaled each tick.
        self._run_duration: dict[str, int] = {}     # sn → locked duration (sec)
        # Track the integer-percent progress value at which we last
        # back-solved duration. Re-solving on every frame caused the countdown
        # to drift while progress sat still — elapsed kept climbing, so
        # `elapsed / (progress/100)` grew with it. We re-solve ONLY when
        # progress ticks to a new integer and keep the prior estimate between
        # transitions. Also used to ignore backward progress jitter.
        self._run_duration_pct: dict[str, int] = {}  # sn → last solved-at progress
        # Last non-spike progress value seen this run. The device's
        # realTimeProgress stream occasionally emits a 0 → 100 → 0 spike in
        # the first ~30s of an Area run. We filter a `progress ≥ 95 while
        # elapsed < 0.9 × duration_seconds` frame as noise and return the
        # last known good value instead, so the PROGRESS pill in Lovelace
        # doesn't flash 100% a minute into a 40-min run.
        self._run_last_progress: dict[str, float] = {}   # sn → last good progress

        # Cadence (hours → seconds)
        opts = entry.options
        self._map_refresh = int(opts.get(CONF_MAP_REFRESH_HOURS, DEFAULT_MAP_REFRESH_HOURS)) * 3600
        self._history_refresh = int(opts.get(CONF_HISTORY_REFRESH_HOURS, DEFAULT_HISTORY_REFRESH_HOURS)) * 3600
        self._reminder_refresh = int(opts.get(CONF_REMINDER_REFRESH_HOURS, DEFAULT_REMINDER_REFRESH_HOURS)) * 3600

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #

    @property
    def devices(self) -> list[dict]:
        """Return the list of discovered Irrisense device dicts."""
        return list(self.api._devices.values())  # noqa: SLF001

    def get_device_data(self, sn: str) -> dict[str, Any]:
        return self._data.setdefault(sn, {})

    def trigger_fast_poll(self, duration: float = DEFAULT_FAST_WINDOW_SECONDS) -> None:
        """Switch to fast polling for the given duration."""
        self._fast_until = max(self._fast_until, time.time() + duration)
        self.update_interval = self._fast_interval
        _LOGGER.debug("Fast-poll window enabled for %ss", duration)

    # ------------------------------------------------------------------ #
    # Update loop
    # ------------------------------------------------------------------ #

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # Dynamic interval: drop back to base once the fast window expires.
        if self._fast_until and time.time() > self._fast_until:
            self._fast_until = 0.0
            self.update_interval = self._base_interval

        try:
            # Refresh device list on every pass (cheap; once a day would be
            # enough, but it also catches new devices being added).
            await self.hass.async_add_executor_job(self.api.get_devices)

            device_registry = dr.async_get(self.hass)
            for dev in self.devices:
                sn = dev.get("sn")
                if not sn:
                    continue
                # Skip devices the user has disabled in HA's device registry.
                dev_entry = device_registry.async_get_device(
                    identifiers={(DOMAIN, sn)}
                )
                if dev_entry is not None and dev_entry.disabled_by is not None:
                    _LOGGER.debug("Skipping disabled device %s in refresh", sn)
                    continue
                await self._refresh_device(sn, dev)

            return self._data
        except Exception as err:
            raise UpdateFailed(f"Irrisense update failed: {err}") from err

    async def _refresh_device(self, sn: str, dev: dict[str, Any]) -> None:
        slot = self._data.setdefault(sn, {})
        slot["equipment"] = dev

        # Always fetch the WR status (light call, per-poll)
        slot["wr_info"] = await self.hass.async_add_executor_job(
            self.api.get_wr_equipment_info, sn
        )

        # Watering setting + task list: every 30 min
        now = time.time()
        if now - self._last_settings_fetch.get(sn, 0) > _SETTINGS_REFRESH_SECONDS:
            slot["setting"] = await self.hass.async_add_executor_job(
                self.api.get_watering_setting, sn
            )
            slot["tasks"] = await self.hass.async_add_executor_job(
                self.api.get_watering_task_list, sn
            )
            self._last_settings_fetch[sn] = now

        # Zone map (S3 JSON): every `map_refresh_hours`. Uses aiohttp to
        # sidestep urllib3's multipart/form-data header parser (see
        # IrrisenseApi.async_fetch_zone_map for the background).
        if now - self._last_map_fetch.get(sn, 0) > self._map_refresh:
            session = async_get_clientsession(self.hass)
            prev_ids = {r.get("id") for r in (slot.get("map") or {}).get("regions", []) if isinstance(r, dict)}
            zmap = await self.api.async_fetch_zone_map(session, sn)
            if isinstance(zmap, dict):
                regions = self.api._parse_regions(zmap)  # noqa: SLF001
                slot["map"] = {"regions": regions, "raw": zmap}
                new_ids = {r.get("id") for r in regions}
                if new_ids != prev_ids:
                    # Zone set changed — let platforms (buttons/numbers) rebuild.
                    async_dispatcher_send(self.hass, SIGNAL_MAP_UPDATED, sn, regions)
            self._last_map_fetch[sn] = now

        # History + stats: every `history_refresh_hours`
        if now - self._last_history_fetch.get(sn, 0) > self._history_refresh:
            slot["stats"] = await self.hass.async_add_executor_job(
                self.api.get_watering_statistics, sn
            )
            slot["history"] = await self.hass.async_add_executor_job(
                self.api.get_watering_history, sn
            )
            self._last_history_fetch[sn] = now

        # Nozzle + reminder: every `reminder_refresh_hours`
        if now - self._last_reminder_fetch.get(sn, 0) > self._reminder_refresh:
            slot["nozzle"] = await self.hass.async_add_executor_job(
                self.api.get_nozzle_type_setting, sn
            )
            slot["reminder"] = await self.hass.async_add_executor_job(
                self.api.get_reminder_setting, sn
            )
            self._last_reminder_fetch[sn] = now

    # ------------------------------------------------------------------ #
    # MQTT integration
    # ------------------------------------------------------------------ #

    def handle_mqtt_message(self, sn: str, data: dict[str, Any]) -> None:
        """Callback invoked (on the MQTT thread) for every received message.

        We stash the parsed payload under `_data[sn]["mqtt"][<bucket>]` and
        schedule a HA-thread update so entities refresh. Any "device is
        working now" hint opens a fast-poll window.
        """
        if not isinstance(data, dict):
            return

        slot = self._data.setdefault(sn, {})
        mqtt_slot = slot.setdefault("mqtt", {})

        topic = data.get("_topic", "")

        # Bucket by topic family so entities can distinguish reports vs. commands
        if "shadow/get/accepted" in topic:
            mqtt_slot["shadow_get"] = data
        elif "shadow/update/accepted" in topic or "shadow/update/documents" in topic:
            mqtt_slot["shadow_update"] = data
        elif "shadow/update/delta" in topic:
            mqtt_slot["shadow_delta"] = data
        elif "WR/cloud/report" in topic:
            mqtt_slot["cloud_report"] = data
        elif topic.endswith("/upChan") or "upChan" in topic:
            # Two possible upChan envelope shapes, tried in order:
            #   1. Legacy assumption: ``{"type": "up_<cmd>", "data": {...}}``
            #      (observed in earlier pool-cleaner integration work).
            #   2. New, symmetric-with-downChan: ``{"<cmd>": {...body}}`` —
            #      the device is expected to echo back in the same shape it
            #      accepts, and every down command the app sends uses form (2).
            #
            # Whichever we find, we normalise to:
            #   * ``cmd_base``: the command name *without* any ``up_`` prefix,
            #     so the ACK watchdog can match against what we sent
            #   * stored dict: always carries a ``data`` key with the inner
            #     body, regardless of input shape — entity code reads
            #     ``msg["data"]`` and must keep working on both.
            cmd_base: str | None = None
            body: dict[str, Any] | None = None
            legacy_type = data.get("type")
            if isinstance(legacy_type, str) and legacy_type:
                cmd_base = legacy_type[3:] if legacy_type.startswith("up_") else legacy_type
                inner = data.get("data")
                body = inner if isinstance(inner, dict) else {}
            else:
                # Pick the first non-underscore top-level key as the cmd name.
                for k, v in data.items():
                    if isinstance(k, str) and not k.startswith("_"):
                        cmd_base = k[3:] if k.startswith("up_") else k
                        body = v if isinstance(v, dict) else {}
                        break

            if cmd_base:
                normalised: dict[str, Any] = {
                    "type": cmd_base,
                    "data": body or {},
                    "_topic": data.get("_topic", topic),
                    "_sn": sn,
                    "_raw": data,
                    # Stamp every upChan frame with arrival time so
                    # `active_zone_state` can pick the freshest source instead
                    # of the highest-priority one. Without this, a stale
                    # `up_setWorkMode` from the previous run (status:1) could
                    # outlive the current run's `up_realTimeProgress`
                    # (status:0) and the banner would keep showing the old
                    # dose label forever.
                    "_ts": time.time(),
                }
                mqtt_slot[f"up_{cmd_base}"] = normalised
                # Clear the ACK watchdog whenever the device echoes a
                # command type we were waiting on.
                try:
                    self.api.note_upchan_ack(sn, cmd_base)
                except Exception:  # noqa: BLE001 - diagnostic only
                    pass
                # The device does NOT echo `up_setWorkMode` for a stop
                # command — completion is signalled by any of the realtime
                # streams transitioning to ``status:0``. Treat that as an
                # ACK for a pending setWorkMode stop so the watchdog stops
                # spamming "ACK TIMEOUT" on every Stop press.
                # We only clear on status:0 (not status:1) because a
                # status:1 frame right after a stop publish would most
                # likely be stale and shouldn't suppress a real timeout.
                if cmd_base in (
                    "realTimeProgress",
                    "realtimeStatus",
                    "workInfoReport",
                    "workInfo",
                ):
                    status_val = (body or {}).get("status")
                    if status_val in (0, "0"):
                        try:
                            self.api.note_upchan_ack(sn, "setWorkMode")
                        except Exception:  # noqa: BLE001
                            pass
                # If the device just reported a new workInfo / setWorkMode ACK,
                # open a fast-poll window so the REST layer catches up quickly.
                if cmd_base in (
                    "setWorkMode",
                    "workInfo",
                    "workInfoReport",
                    "realtimeStatus",
                    "realTimeProgress",
                    "realTimeProgressReport",
                ):
                    self.trigger_fast_poll()
            else:
                mqtt_slot["up_raw"] = data
        else:
            mqtt_slot.setdefault("other", []).append({"topic": topic, "data": data})

        # Push updated data to entities (thread-safe).
        self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, self._data)

    # ------------------------------------------------------------------ #
    # Command wrappers (used by services + entity actions)
    # ------------------------------------------------------------------ #

    async def async_start_zone(
        self,
        sn: str,
        map_id: int,
        *,
        region_type: int | None = None,
        water_yield: float | None = None,
        point_time: int | None = None,
        pesticide: bool = False,
    ) -> bool:
        """Start a zone. Any parameter the caller omits is auto-resolved from
        the cached zone map (``slot['map']['regions']``).

        Resolution order, per field:
          * ``region_type``: from the region's ``type`` (0=Area, 1=Line, 2=Point)
          * ``water_yield`` (Area/Line): from the region's ``waterYield``,
            falling back to ``WATER_YIELD_LOW`` (0.1 = UI "3 mm")
          * ``point_time`` (Point): from the region's ``pointTime``,
            falling back to ``POINT_TIME_LOW`` (1 minute)

        Before publishing we snap off-preset values to the nearest firmware-
        accepted preset. The device silently drops values outside
        {0.1, 0.25, 0.5} for waterYield and {1, 5, 10} for point_time
        (APK line 1844/1846 — no other values are reachable via the app).
        """
        region = self._region_for(sn, map_id)

        resolved_type = region_type
        if resolved_type is None:
            resolved_type = int(region.get("type", 0)) if region else 0

        kwargs: dict[str, Any] = {"region_type": resolved_type, "pesticide": pesticide}

        if resolved_type == REGION_TYPE_POINT:
            if point_time is None:
                point_time = int(region.get("pointTime", POINT_TIME_LOW)) if region else POINT_TIME_LOW
            kwargs["point_time"] = _snap_to_preset(
                int(point_time), POINT_TIME_PRESETS, "point_time (minutes)",
            )
        else:
            if water_yield is None:
                water_yield = float(region.get("waterYield", WATER_YIELD_LOW)) if region else WATER_YIELD_LOW
            kwargs["water_yield"] = _snap_to_preset(
                float(water_yield), WATER_YIELD_PRESETS, "waterYield",
            )

        _LOGGER.debug(
            "async_start_zone resolved sn=%s map_id=%s region=%s → kwargs=%s",
            sn, map_id, region, kwargs,
        )

        ok = await self.hass.async_add_executor_job(
            lambda: self.api.start_zone(sn, map_id, **kwargs)
        )
        if ok:
            self.trigger_fast_poll()
            # Also ask the device for an immediate work snapshot.
            await self.hass.async_add_executor_job(self.api.query_work_info, sn)
        return ok

    def _region_for(self, sn: str, map_id: int) -> dict[str, Any] | None:
        for r in self.zones_for(sn):
            if r.get("id") == map_id:
                return r
        return None

    async def async_stop_zone(self, sn: str, map_id: int) -> bool:
        """Stop the active zone, retrying up to 3 times.

        The device occasionally misses (or silently drops) a
        `setWorkMode stop` publish, especially when it arrives mid-frame of
        its own realTimeProgress stream. Symptom: user has to tap Stop two
        or three times in the UI. We issue the stop, ask for a fresh
        workInfo snapshot, re-read `active_zone_state`, and re-issue the
        stop up to two more times if the device still reports running.

        Returns True if the publish succeeded AND the device reports idle
        within ~10s total. Returns True-with-warning if the publishes
        succeeded but the device is still running (avoids scaring the UI
        when MQTT round-trip is just slow).
        """
        max_attempts = 3
        last_publish_ok = False
        for attempt in range(1, max_attempts + 1):
            last_publish_ok = await self.hass.async_add_executor_job(
                self.api.stop_zone, sn, map_id
            )
            if not last_publish_ok:
                _LOGGER.warning(
                    "Stop attempt %d/%d for sn=%s map_id=%s failed to publish",
                    attempt, max_attempts, sn, map_id,
                )
                # Publish itself failed — wait briefly and try again.
                await asyncio.sleep(1.0)
                continue

            self.trigger_fast_poll()
            # Give the device time to react, then ask for a fresh snapshot.
            await asyncio.sleep(1.5)
            await self.hass.async_add_executor_job(self.api.query_work_info, sn)
            await asyncio.sleep(1.5)

            state = self.active_zone_state(sn)
            if not state or not state.get("is_running"):
                if attempt > 1:
                    _LOGGER.info(
                        "Stop for sn=%s map_id=%s succeeded on attempt %d/%d",
                        sn, map_id, attempt, max_attempts,
                    )
                return True

            still = state.get("zone_id")
            _LOGGER.warning(
                "Stop attempt %d/%d: sn=%s still reports zone %s running — retrying",
                attempt, max_attempts, sn, still,
            )

        return last_publish_ok

    async def async_set_schedule_enabled(
        self, sn: str, task_ids: list[int], enabled: bool
    ) -> bool:
        ok = await self.hass.async_add_executor_job(
            self.api.set_schedule_enabled, sn, task_ids, enabled
        )
        if ok:
            # Force a settings refresh on next poll.
            self._last_settings_fetch.pop(sn, None)
            await self.async_request_refresh()
        return ok

    async def async_set_nozzle_type(self, sn: str, nozzle_type: int) -> bool:
        ok = await self.hass.async_add_executor_job(
            self.api.set_nozzle_type, sn, nozzle_type
        )
        if ok:
            self._last_reminder_fetch.pop(sn, None)
            await self.async_request_refresh()
        return ok

    async def async_set_watering_setting(self, sn: str, settings: dict[str, Any]) -> bool:
        ok = await self.hass.async_add_executor_job(
            self.api.set_watering_setting, sn, settings
        )
        if ok:
            self._last_settings_fetch.pop(sn, None)
            await self.async_request_refresh()
        return ok

    async def async_set_reminder(self, sn: str, key: str, enabled: bool) -> bool:
        """Toggle one of the four reminder settings.

        Routes to the correct `wr/update*ReminderSetting` endpoint based on
        the reminder key from `getReminderSetting`.
        """
        setter_map = {
            "drainageReminder": self.api.set_drainage_reminder,
            "pesticideReminder": self.api.set_pesticide_reminder,
            "taskReminder": self.api.set_task_reminder,
            "waterShortageReminder": self.api.set_water_shortage_reminder,
        }
        fn = setter_map.get(key)
        if fn is None:
            _LOGGER.warning("Unknown reminder key: %s", key)
            return False
        ok = await self.hass.async_add_executor_job(fn, sn, enabled)
        if ok:
            self._last_reminder_fetch.pop(sn, None)
            await self.async_request_refresh()
        return ok

    # ------------------------------------------------------------------ #
    # Dashboard selection state (controls card)
    # ------------------------------------------------------------------ #

    def get_zone_selection(self, sn: str) -> int | None:
        """Return the zone_id currently highlighted in the Zone select.

        Falls back to the first available zone on the device so a fresh
        install (or a restart before RestoreEntity fires) still has a
        sensible selection for the Dose select to render against.
        """
        sel = self._zone_selection.get(sn)
        if sel is not None:
            # Make sure the selection is still a valid zone
            if self._region_for(sn, sel) is not None:
                return sel
        zones = self.zones_for(sn)
        if zones:
            first = zones[0].get("id")
            if isinstance(first, int):
                return first
        return None

    def set_zone_selection(self, sn: str, zone_id: int) -> None:
        """Persist the user's zone pick and reset dose to the type default.

        Resetting dose on zone change matches the Aiper app (picking a new
        zone re-shows its three presets with the lowest highlighted) and
        guarantees the stored label is always valid for the zone's type.
        """
        self._zone_selection[sn] = int(zone_id)
        region = self._region_for(sn, zone_id)
        rtype = int(region.get("type", 0)) if region else 0
        self._dose_selection[sn] = default_dose_label_for_region_type(rtype)
        async_dispatcher_send(self.hass, SIGNAL_SELECTION_CHANGED, sn)

    def get_dose_selection(self, sn: str) -> str | None:
        """Return the currently-picked dose/duration label ("3 mm" / "5 min")."""
        return self._dose_selection.get(sn)

    def set_dose_selection(self, sn: str, label: str) -> None:
        """Persist the user's dose pick. Label must be one of the six presets."""
        self._dose_selection[sn] = label

    def selected_region_type(self, sn: str) -> int:
        """Convenience for the DoseSelect: 0/1=Area/Line, 2=Point, 0 if unknown."""
        zid = self.get_zone_selection(sn)
        if zid is None:
            return 0
        r = self._region_for(sn, zid)
        return int(r.get("type", 0)) if r else 0

    # ------------------------------------------------------------------ #
    # Active-zone snapshot (what the device is currently doing)
    # ------------------------------------------------------------------ #

    # Fields we check for the "is this zone running" signal, in freshness
    # order. realTimeProgress is the fastest-updating stream so it takes
    # precedence; setWorkMode echoes are also rich but less frequent.
    _ACTIVE_SOURCES = (
        "up_realTimeProgress",
        "up_realtimeStatus",
        "up_setWorkMode",
        "up_workInfoReport",
        "up_workInfo",
    )

    def active_zone_state(self, sn: str) -> dict[str, Any] | None:
        """Return a live snapshot of what the device is watering right now.

        Selection rule is "most recently arrived status:1 frame". If the
        freshest frame across all sources is ``status:0``, the device is
        idle and we return None even if older frames still sit in the
        mqtt slot with ``status:1``. This prevents the banner from showing
        the previous run's dose / elapsed after the device has stopped.

        Keys returned (running):
          ``is_running``        bool
          ``zone_id``           int
          ``zone_name``         str | None
          ``region_type``       int | None   (0/1/2)
          ``dose_label``        str | None   ("3 mm" / "5 min" / ...)
          ``water_yield``       float | None (raw wire value)
          ``point_time``        int | None   (raw wire value, minutes)
          ``time_sec``          int | None   (elapsed seconds, from `time`)
          ``progress``          float | None (device-reported 0..1 or 0..100)
        """
        mqtt = (self._data.get(sn) or {}).get("mqtt") or {}

        # Find the most-recent frame across all sources (regardless of status).
        freshest_key: str | None = None
        freshest_ts: float = -1.0
        freshest_msg: dict[str, Any] | None = None
        for key in self._ACTIVE_SOURCES:
            msg = mqtt.get(key)
            if not isinstance(msg, dict):
                continue
            ts = msg.get("_ts", 0.0)
            if not isinstance(ts, (int, float)):
                ts = 0.0
            if ts > freshest_ts:
                freshest_ts = float(ts)
                freshest_key = key
                freshest_msg = msg

        if freshest_msg is None:
            # No frames at all — clear any stale run-start stamp.
            self._run_start_ts.pop(sn, None)
            self._run_start_zone.pop(sn, None)
            self._run_duration.pop(sn, None)
            self._run_duration_pct.pop(sn, None)
            self._run_last_progress.pop(sn, None)
            return None

        body = freshest_msg.get("data") if isinstance(freshest_msg.get("data"), dict) else {}
        if not isinstance(body, dict):
            self._run_start_ts.pop(sn, None)
            self._run_start_zone.pop(sn, None)
            self._run_duration.pop(sn, None)
            self._run_duration_pct.pop(sn, None)
            self._run_last_progress.pop(sn, None)
            return None
        # Working-status set is {1, 2}, confirmed from APK
        # `IrrisenseDeviceInfoSourceMemory.Companion.workingStatus()`:
        #   return new Integer[]{1, 2};
        status = body.get("status")
        if status in (0, "0"):
            # Device just stopped. Don't report any of the older working
            # frames as "still running" — they're stale by construction.
            self._run_start_ts.pop(sn, None)
            self._run_start_zone.pop(sn, None)
            self._run_duration.pop(sn, None)
            self._run_duration_pct.pop(sn, None)
            self._run_last_progress.pop(sn, None)
            return None
        if status not in (1, "1", 2, "2"):
            # Unknown status — safest to treat as not-running.
            self._run_start_ts.pop(sn, None)
            self._run_start_zone.pop(sn, None)
            self._run_duration.pop(sn, None)
            self._run_duration_pct.pop(sn, None)
            self._run_last_progress.pop(sn, None)
            return None

        # Zone id lookup handles *two* wire shapes, confirmed from APK
        # `IrrisenseDeviceInfoSourceMemory.java`:
        #   - `workInfo` / `workInfoReport`     → only carries status + mode
        #     (zone id is NOT in these frames at all)
        #   - `setWorkMode` echo                → top-level `map_id`
        #   - `realTimeProgress(Report)`        → nested `map_info.id`
        zone_id = _extract_map_id(body)
        if zone_id is None:
            return None

        region = self._region_for(sn, zone_id)
        region_type = int(region.get("type", 0)) if region else None

        # For fields that the freshest frame may not carry (e.g. setWorkMode
        # echo has no `time` or `progress`, workInfoReport has only status/
        # mode), fall back to the most recent realTimeProgress-family frame
        # with the same zone id — it's the richest source for those fields.
        #
        # The same-zone match uses `_extract_map_id`, which handles BOTH
        # `map_id` (top-level, emitted by setWorkMode echoes) AND
        # `map_info.id` (nested, emitted by realTimeProgress frames).
        def _fallback(key: str) -> Any:
            if key in body and isinstance(body.get(key), (int, float)):
                return body.get(key)
            # Scan other sources for the same zone, pick the freshest that
            # has this field set.
            best_ts = -1.0
            best_val: Any = None
            for other_key in self._ACTIVE_SOURCES:
                om = mqtt.get(other_key)
                if not isinstance(om, dict):
                    continue
                ob = om.get("data") if isinstance(om.get("data"), dict) else {}
                if not isinstance(ob, dict):
                    continue
                if ob.get("status") not in (1, "1", 2, "2"):
                    continue
                # Require the same zone — otherwise we'd mix runs.
                other_zone = _extract_map_id(ob)
                if other_zone is None or other_zone != zone_id:
                    continue
                val = ob.get(key)
                if not isinstance(val, (int, float)):
                    continue
                ots = om.get("_ts", 0.0)
                if isinstance(ots, (int, float)) and ots > best_ts:
                    best_ts = float(ots)
                    best_val = val
            return best_val

        elapsed = _fallback("time")
        progress = _fallback("progress")

        # Route `waterYield` and `point_time` through `_fallback` too.
        # During the first ~1s of a run, the freshest same-zone frame is a
        # `setWorkMode` echo, which carries `waterYield` but NOT `point_time`.
        # Using `_fallback` lets us pull `point_time` from a stale (but
        # same-zone, same-run) realTimeProgress frame that's still cached.
        wy = _fallback("waterYield")
        pt = _fallback("point_time")
        if pt is None:
            pt = _fallback("pointTime")
        # Final fallback — the region cache (from S3 zone-map JSON) always
        # carries the user's configured pointTime/waterYield defaults. This
        # means point_time×60 is ALWAYS available for known zones.
        if region:
            if pt is None:
                pt = region.get("pointTime") or region.get("point_time")
            if wy is None:
                wy = region.get("waterYield")
        if region_type == REGION_TYPE_POINT:
            dose_label = label_for_point_time(pt)
        else:
            dose_label = label_for_water_yield(wy)

        # Stamp a stable start time for the *current* run. First time we see
        # a given (sn, zone_id) running, we back-date the start by whatever
        # `elapsed` the device already reported (usually 1-2s by the time
        # the first MQTT frame lands). If the user starts a different zone,
        # we re-stamp. When the device stops, we clear.
        #
        # Keeping this on the coordinator (not in the sensor) means the
        # timestamp survives attribute rerenders — the bar won't jitter
        # each time `elapsed` ticks up by a second.
        prior_zone = self._run_start_zone.get(sn)
        if prior_zone != zone_id:
            elapsed_seed = elapsed if isinstance(elapsed, (int, float)) else 0
            self._run_start_ts[sn] = time.time() - float(elapsed_seed)
            self._run_start_zone[sn] = zone_id
            # New run → drop any locked duration from the prior run.
            self._run_duration.pop(sn, None)
            self._run_duration_pct.pop(sn, None)
            # New run → drop last-good-progress anchor.
            self._run_last_progress.pop(sn, None)
        start_ts = self._run_start_ts.get(sn)

        # Best-effort total run duration, in seconds. We keep this *always
        # non-None when running* so timer-bar-card (which dies on a
        # non-numeric duration) has something to render against.
        #
        # Order of preference:
        #   1. Back-solve from elapsed / progress (most trustworthy signal
        #      we actually have for Area/Line zones — those are volume-
        #      based, so the device doesn't report a real total-time up
        #      front).
        #   2. Point zones: convert point_time (minutes) × n_points → seconds.
        #   3. A body key named like total_time / totalTime / duration, BUT
        #      only if it's in a plausible single-run range (10s..6h). The
        #      device has been seen emitting huge numbers (e.g. 8e9) under
        #      keys like `total` / `total_time` — probably a cumulative
        #      counter or microsecond value, definitely not a per-run
        #      duration.
        #   4. Last-resort 300s placeholder (enough to not blow up the UI).
        #
        # Area zones: the device reports `progress` erratically — within a
        # few seconds we've seen 0% → 100% → 2% → 6%. Back-solving elapsed /
        # progress on every frame makes `duration_seconds` oscillate
        # between 60s and ~6000s, which makes timer-bar-card re-scale on
        # every update. We LATCH the first good duration we compute for a
        # given (sn, zone) run and reuse it for the rest of the run. We
        # still honor an explicit total_time / totalTime / duration key if
        # present — those don't drift.
        DUR_MIN, DUR_MAX = 10, 6 * 3600  # 10s .. 6h
        LATCH_PCT = 5  # progress% at which a back-solve is locked for the run

        # Fast path: we already locked a duration for this run (Point zones,
        # an explicit body-total, or back-solve at progress ≥ LATCH_PCT).
        # `_run_duration_pct == LATCH_PCT` is the sentinel meaning "latched".
        last_solve_pct = self._run_duration_pct.get(sn, -1)
        duration_seconds: int | None = None
        if last_solve_pct >= LATCH_PCT:
            duration_seconds = self._run_duration.get(sn)

        # --- step (1) Point zones: pointTime × n_points × 60 -------------
        if duration_seconds is None and (
            region_type == REGION_TYPE_POINT
            and isinstance(pt, (int, float)) and pt > 0
        ):
            n_pts = 0
            if region:
                n_pts = int(region.get("n_points") or 0)
            if n_pts > 0:
                cand = int(pt) * n_pts * 60
                if DUR_MIN <= cand <= DUR_MAX:
                    duration_seconds = cand
                    self._run_duration[sn] = duration_seconds
                    self._run_duration_pct[sn] = LATCH_PCT  # latch

        # --- step (2) back-solve elapsed / progress ----------------------
        # Re-solve ONLY when `progress` ticks to a higher integer percent
        # than we last solved at. This kills two jitter sources:
        #
        #   (a) Elapsed climbs while `progress` sits still on a single
        #       integer value (e.g. progress=3% for 30s as elapsed grows
        #       90→120). Per-frame re-solve made `elapsed / 0.03` stretch
        #       from 3000 to 4000s — the countdown appeared to grow, not
        #       shrink. Now we freeze the estimate between transitions.
        #
        #   (b) Backward progress noise (e.g. 4% → 3% due to device
        #       re-baselining). The `progress > last_solve_pct` gate
        #       ignores those frames — the `>=` comparison would let equal
        #       values through and re-trigger (a).
        #
        # Latch-permanent condition: progress ≥ LATCH_PCT. Below that we
        # keep refining as progress advances. Progress always tied to an
        # ever-growing elapsed so the estimate improves monotonically in
        # accuracy (quantization error drops as the denominator grows).
        if duration_seconds is None and (
            isinstance(elapsed, (int, float))
            and isinstance(progress, (int, float))
            and progress > 0
            and elapsed >= 15
        ):
            # Device sends progress as integer percent in [0, 100]. The
            # `< 1` branch is defensive for firmware that might someday
            # emit fractional 0–1 values.
            prog_pct = int(progress) if progress >= 1 else int(progress * 100)
            if 1 <= prog_pct <= 94 and prog_pct > last_solve_pct:
                frac = prog_pct / 100.0
                cand = int(elapsed / frac)
                if DUR_MIN <= cand <= DUR_MAX:
                    duration_seconds = max(cand, 60)
                    self._run_duration[sn] = duration_seconds
                    # Latch permanently once progress is solid enough that
                    # ±1% quantization won't swing the estimate dramatically.
                    self._run_duration_pct[sn] = (
                        LATCH_PCT if prog_pct >= LATCH_PCT else prog_pct
                    )
            elif self._run_duration.get(sn) is not None:
                # progress hasn't advanced — keep the prior back-solve.
                duration_seconds = self._run_duration.get(sn)

        # --- step (3) explicit body-level total --------------------------
        if duration_seconds is None:
            for key in ("total_time", "totalTime", "duration"):
                val = body.get(key)
                if isinstance(val, (int, float)) and DUR_MIN <= val <= DUR_MAX:
                    duration_seconds = int(val)
                    self._run_duration[sn] = duration_seconds
                    self._run_duration_pct[sn] = LATCH_PCT  # latch
                    break

        # --- placeholder while we wait for progress ----------------------
        # 300s unlatched — next frame will recompute. We also flag this so
        # the dashboard can render "--:--" instead of a misleading countdown
        # (the bar otherwise ticks 5:00 → 4:59 → ... on the fake 300s).
        duration_pending = duration_seconds is None
        if duration_seconds is None:
            duration_seconds = 300

        # Progress spike filter. The device's realTimeProgress stream
        # occasionally emits a transient 100% reading in the first ~30-90s
        # of an Area run. A 100% that shows up while we still have 90%+ of
        # the latched duration left to burn is always noise, never a real
        # end-of-run. Suppress by returning the last non-spike progress we
        # saw instead — keeps the Lovelace PROGRESS pill smooth without
        # swallowing a legitimate end-of-run 100%.
        #
        # `elapsed < 0.9 × duration_seconds` passes real end-of-run frames
        # through untouched.
        if (
            isinstance(progress, (int, float))
            and isinstance(elapsed, (int, float))
            and duration_seconds > 0
            and not duration_pending  # don't filter while on the 300s placeholder
        ):
            prog_pct = float(progress) if progress > 1 else float(progress) * 100.0
            if prog_pct >= 95 and elapsed < duration_seconds * 0.9:
                # Noise spike — prefer last known good (may be None at the
                # very start of a run, in which case the sensor returns None
                # and Lovelace renders the pill as "—" for one frame).
                progress = self._run_last_progress.get(sn)
            else:
                # Remember this as the last trustworthy reading.
                self._run_last_progress[sn] = float(progress)

        # Emit a second duration representation in `H:MM:SS` format.
        # `timer-bar-card` (rianadon) does NOT accept an integer-seconds
        # attribute — it tries to parse the value as a HH:MM:SS string and
        # errors with "Could not convert duration: 300 is not of format
        # 0:10:00." TimeFlow-Card uses `duration_seconds` directly via Jinja,
        # so both libraries are covered by shipping both shapes.
        _h, _rem = divmod(int(duration_seconds), 3600)
        _m, _s = divmod(_rem, 60)
        duration_hms = f"{_h}:{_m:02d}:{_s:02d}"

        # Surface the sprinkler-motion + pressure fields the APK's
        # `realTimeProgress` handler publishes:
        #   * `x`, `y`           — head position in the zone's local coord
        #                          system (updates every realTimeProgress
        #                          frame).
        #   * `repairLayer`      — coverage-pass counter (increments as the
        #                          head re-sweeps the zone).
        #   * `waterpress`       — live line pressure.
        rl_val = _fallback("repairLayer")

        return {
            "is_running": True,
            "zone_id": zone_id,
            "zone_name": self.zone_name(sn, zone_id),
            "region_type": region_type,
            "dose_label": dose_label,
            "water_yield": wy if isinstance(wy, (int, float)) else None,
            "point_time": pt if isinstance(pt, (int, float)) else None,
            "time_sec": elapsed,
            "progress": progress,
            "x": _fallback("x"),
            "y": _fallback("y"),
            "repair_layer": rl_val,
            "water_pressure": _fallback("waterpress"),
            "source": freshest_key,
            "source_ts": freshest_ts,
            "start_ts": start_ts,
            "duration_seconds": duration_seconds,
            "duration_hms": duration_hms,
            # True while we're on the unconfirmed 300s placeholder.
            # Dashboard renders Time Remaining as "--:--" instead of an
            # honest-looking-but-fake countdown.
            "duration_pending": duration_pending,
        }

    # ------------------------------------------------------------------ #
    # Utility: zones for a device
    # ------------------------------------------------------------------ #

    def zones_for(self, sn: str) -> list[dict[str, Any]]:
        """Return the list of zone (region) dicts from the cached zone map."""
        slot = self._data.get(sn) or {}
        zmap = slot.get("map") or {}
        regions = zmap.get("regions") if isinstance(zmap, dict) else None
        return regions if isinstance(regions, list) else []

    def zone_name(self, sn: str, map_id: int) -> str | None:
        for r in self.zones_for(sn):
            if r.get("id") == map_id:
                name = r.get("name")
                if isinstance(name, str) and name:
                    return name
        return None
