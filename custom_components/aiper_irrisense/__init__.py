"""Aiper Irrisense 2 — Home Assistant integration entry point."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
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

    # Auth + device discovery on the executor
    ok = await hass.async_add_executor_job(api.login)
    if not ok:
        return False

    devices = await hass.async_add_executor_job(api.get_devices)
    if not devices:
        _LOGGER.warning("No Irrisense (WRX/WGX) devices found on this account")

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

    # MQTT (optional; on by default)
    if entry.options.get(CONF_ENABLE_MQTT, True):
        mqtt_ok = await hass.async_add_executor_job(api.connect_mqtt)
        if mqtt_ok:
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
        else:
            _LOGGER.warning("Irrisense MQTT connect failed — realtime disabled")

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
