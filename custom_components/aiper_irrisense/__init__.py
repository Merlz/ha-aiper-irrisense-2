"""Aiper Irrisense 2 — Home Assistant integration entry point."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api import IrrisenseApi
from .const import (
    CONF_ENABLE_MQTT,
    CONF_MQTT_DEBUG,
    CONF_REGION,
    DOMAIN,
)
from .coordinator import IrrisenseCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.WEATHER,
    Platform.IMAGE,
]
# Dose lives on the Watering Dose select (label-valued: "3 mm" / "5 min" / ...)
# and backend mapping happens in button.StartWateringButton.

SERVICE_START_ZONE = "start_zone"
SERVICE_STOP_ZONE = "stop_zone"
SERVICE_QUERY_WORK_INFO = "query_work_info"
SERVICE_DEBUG_PUBLISH = "debug_publish"

ATTR_SN = "sn"
ATTR_ZONE_ID = "zone_id"
ATTR_REGION_TYPE = "region_type"
ATTR_WATER_YIELD = "water_yield"
ATTR_POINT_TIME = "point_time"
ATTR_PESTICIDE = "pesticide"
ATTR_TOPIC = "topic"
ATTR_PAYLOAD = "payload"
ATTR_QOS = "qos"

START_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SN): cv.string,
        vol.Required(ATTR_ZONE_ID): vol.Coerce(int),
        # region_type is optional — omitted means "auto-resolve from the
        # cached zone map". Override is still accepted for power users.
        vol.Optional(ATTR_REGION_TYPE): vol.All(
            vol.Coerce(int), vol.In([0, 1, 2])
        ),
        vol.Optional(ATTR_WATER_YIELD): vol.Coerce(float),
        vol.Optional(ATTR_POINT_TIME): vol.Coerce(int),
        vol.Optional(ATTR_PESTICIDE, default=False): cv.boolean,
    }
)

STOP_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SN): cv.string,
        vol.Required(ATTR_ZONE_ID): vol.Coerce(int),
    }
)

QUERY_WORK_SCHEMA = vol.Schema({vol.Required(ATTR_SN): cv.string})

DEBUG_PUBLISH_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SN): cv.string,
        vol.Required(ATTR_TOPIC): cv.string,
        vol.Required(ATTR_PAYLOAD): cv.string,
        vol.Optional(ATTR_QOS, default=1): vol.All(vol.Coerce(int), vol.In([0, 1])),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Aiper Irrisense 2 account (one config entry = one account)."""
    api = IrrisenseApi(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        region=entry.data.get(CONF_REGION, "eu"),
    )
    api.mqtt_debug = bool(entry.options.get(CONF_MQTT_DEBUG, False))

    # Auth + device discovery on the executor. Wrapped in asyncio.wait_for
    # so a slow cloud round-trip cannot push the setup coroutine past HA's
    # 60s bootstrap window. ConfigEntryNotReady triggers HA's exponential
    # backoff retry rather than the no-retry SETUP_ERROR path. See #11.
    try:
        # login() is a two-call chain (/login then the OpenID-token fetch),
        # each an executor request that can take up to ~30s with backoff, so
        # 15s was too tight on a slow cloud round-trip and tripped a spurious
        # ConfigEntryNotReady. 30s + 20s keeps worst-case setup under HA's 60s
        # bootstrap window while matching what these calls actually need.
        await asyncio.wait_for(
            hass.async_add_executor_job(api.login), timeout=30
        )
        devices = await asyncio.wait_for(
            hass.async_add_executor_job(api.get_devices), timeout=20
        )
    except TimeoutError as ex:
        raise ConfigEntryNotReady(
            f"Aiper cloud timeout during setup (check network): {ex}"
        ) from ex
    except Exception as ex:  # noqa: BLE001 - api.login distinguishes auth vs other via message text
        # api.login raises Exception("Login failed: ...") on cloud-rejected
        # credentials and Exception("No token in login response: ...") on
        # protocol errors. Both are permanent config problems that should
        # trigger HA's reauth flow rather than infinite ConfigEntryNotReady
        # backoff. Discriminate by message prefix; anything else falls
        # back to transient cloud-error retry.
        if "Login failed" in str(ex) or "No token" in str(ex):
            raise ConfigEntryAuthFailed(
                f"Aiper authentication failed: {ex}"
            ) from ex
        raise ConfigEntryNotReady(
            f"Aiper cloud error during setup: {ex}"
        ) from ex

    if not devices:
        _LOGGER.warning("No Irrisense (WR/WG/WC/WL) devices found on this account")

    # Filter out devices the user has disabled in HA's device registry.
    # Devices not yet in the registry are let through so first-time setup
    # registers them; subsequent reloads honour the user's disable.
    device_registry = dr.async_get(hass)

    def _is_enabled(sn: str) -> bool:
        if not sn:
            return False
        dev_entry = device_registry.async_get_device(identifiers={(DOMAIN, sn)})
        if dev_entry is not None and dev_entry.disabled_by is not None:
            _LOGGER.info(
                "Skipping disabled device %s (disabled_by=%s)",
                sn, dev_entry.disabled_by,
            )
            return False
        return True

    devices = [d for d in devices if _is_enabled(d.get("sn", ""))]

    coordinator = IrrisenseCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()

    # MQTT (optional; on by default) — runs as an entry-bound background
    # task so a slow AWS IoT connect (~30s blocking) plus per-device
    # subscribe loop do not delay the setup coroutine. The integration
    # comes up REST-poll-only; MQTT real-time updates join in the
    # background as soon as connect + subscribes complete. See #11.
    # Skipped entirely if the account has no Irrisense devices —
    # connecting MQTT to subscribe to nothing is a 30s waste.
    if entry.options.get(CONF_ENABLE_MQTT, True) and devices:
        entry.async_create_background_task(
            hass,
            _async_setup_mqtt_background(hass, api, devices, coordinator),
            f"aiper_irrisense_mqtt_setup_{entry.entry_id}",
        )

    # Register devices in the device registry.
    device_registry = dr.async_get(hass)
    for dev in devices:
        sn = dev.get("sn")
        if not sn:
            continue
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, sn)},
            manufacturer="Aiper",
            model=dev.get("modelName") or "Irrisense 2",
            name=dev.get("name") or f"Irrisense {sn}",
            sw_version=dev.get("firmwareVersion") or dev.get("version"),
            serial_number=sn,
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload on options change
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _register_services(hass)
    return True


async def _async_setup_mqtt_background(
    hass: HomeAssistant,
    api: IrrisenseApi,
    devices: list[dict[str, Any]],
    coordinator: IrrisenseCoordinator,
) -> None:
    """MQTT connect + per-device subscribe, off the setup path.

    Issue #11: AWS IoT `connect_mqtt` is up to 30s blocking
    (`configureConnectDisconnectTimeout(30)`) and the per-device
    subscribe / query_work_info / request_shadow loop adds several
    seconds on top. Together they push the setup coroutine over HA's
    60s bootstrap window. Running this off the setup path keeps the
    integration responsive: entities come online via REST-only
    polling first, MQTT layers on as soon as the connect resolves.
    """
    try:
        mqtt_ok = await hass.async_add_executor_job(api.connect_mqtt)
        if not mqtt_ok:
            _LOGGER.warning("Irrisense MQTT connect failed — realtime disabled")
            return
        for dev in devices:
            sn = dev.get("sn")
            if not sn:
                continue
            await hass.async_add_executor_job(
                api.subscribe_device, sn, coordinator.handle_mqtt_message
            )
            # Nudge the device to report current state.
            await hass.async_add_executor_job(api.query_work_info, sn)
            await hass.async_add_executor_job(api.request_shadow, sn)
    except Exception:  # noqa: BLE001 - MQTT is optional; don't crash integration load
        _LOGGER.exception("Irrisense MQTT background setup failed")


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down an account."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    slot = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if slot:
        api: IrrisenseApi = slot["api"]
        await hass.async_add_executor_job(api.disconnect)
    # Only drop the services on the last entry
    if not hass.data.get(DOMAIN):
        for svc in (SERVICE_START_ZONE, SERVICE_STOP_ZONE, SERVICE_QUERY_WORK_INFO, SERVICE_DEBUG_PUBLISH):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)
    return unload_ok


# ---------------------------------------------------------------------- #
# Services
# ---------------------------------------------------------------------- #


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_START_ZONE):
        return

    def _find_coordinator(sn: str) -> IrrisenseCoordinator | None:
        for slot in hass.data.get(DOMAIN, {}).values():
            coord: IrrisenseCoordinator = slot["coordinator"]
            if sn in (d.get("sn") for d in coord.devices):
                return coord
        return None

    async def _svc_start_zone(call: ServiceCall) -> None:
        sn = call.data[ATTR_SN]
        coord = _find_coordinator(sn)
        if not coord:
            _LOGGER.error("start_zone: unknown SN %s", sn)
            return
        region_type = call.data.get(ATTR_REGION_TYPE)
        await coord.async_start_zone(
            sn,
            int(call.data[ATTR_ZONE_ID]),
            region_type=int(region_type) if region_type is not None else None,
            water_yield=call.data.get(ATTR_WATER_YIELD),
            point_time=call.data.get(ATTR_POINT_TIME),
            pesticide=bool(call.data.get(ATTR_PESTICIDE, False)),
        )

    async def _svc_stop_zone(call: ServiceCall) -> None:
        sn = call.data[ATTR_SN]
        coord = _find_coordinator(sn)
        if not coord:
            _LOGGER.error("stop_zone: unknown SN %s", sn)
            return
        await coord.async_stop_zone(sn, int(call.data[ATTR_ZONE_ID]))

    async def _svc_query_work(call: ServiceCall) -> None:
        sn = call.data[ATTR_SN]
        coord = _find_coordinator(sn)
        if not coord:
            return
        await hass.async_add_executor_job(coord.api.query_work_info, sn)

    async def _svc_debug_publish(call: ServiceCall) -> None:
        """Diagnostic: publish arbitrary bytes to an arbitrary MQTT topic on
        the device's MQTT connection. Used to experiment with payload shapes
        while reverse-engineering setWorkMode acceptance.
        """
        sn = call.data[ATTR_SN]
        coord = _find_coordinator(sn)
        if not coord:
            _LOGGER.error("debug_publish: unknown SN %s", sn)
            return
        await hass.async_add_executor_job(
            coord.api.debug_publish,
            call.data[ATTR_TOPIC],
            call.data[ATTR_PAYLOAD],
            int(call.data.get(ATTR_QOS, 1)),
        )

    hass.services.async_register(DOMAIN, SERVICE_START_ZONE, _svc_start_zone, schema=START_ZONE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_ZONE, _svc_stop_zone, schema=STOP_ZONE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_QUERY_WORK_INFO, _svc_query_work, schema=QUERY_WORK_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DEBUG_PUBLISH, _svc_debug_publish, schema=DEBUG_PUBLISH_SCHEMA)
