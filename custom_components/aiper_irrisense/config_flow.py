"""Config flow for the Aiper Irrisense 2 integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .api import IrrisenseApi
from .const import (
    CONF_ENABLE_EXPERIMENTAL_SENSORS,
    CONF_ENABLE_MQTT,
    CONF_ENABLE_WEATHER,
    CONF_HISTORY_REFRESH_HOURS,
    CONF_MAP_REFRESH_HOURS,
    CONF_MQTT_DEBUG,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_REMINDER_REFRESH_HOURS,
    CONF_WEATHER_REFRESH_HOURS,
    DEFAULT_ENABLE_WEATHER,
    DEFAULT_HISTORY_REFRESH_HOURS,
    DEFAULT_MAP_REFRESH_HOURS,
    DEFAULT_REMINDER_REFRESH_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEATHER_REFRESH_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default="eu"): vol.In(
            {"us": "Americas", "eu": "Europe", "asia": "Asia/Pacific"}
        ),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Log in and count Irrisense devices on the account."""
    api = IrrisenseApi(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        region=data[CONF_REGION],
    )
    try:
        ok = await hass.async_add_executor_job(api.login)
        if not ok:
            raise InvalidAuth

        devices = await hass.async_add_executor_job(api.get_devices)
    except InvalidAuth:
        raise
    except Exception as err:
        _LOGGER.error("Irrisense login validation failed: %s", err)
        raise InvalidAuth from err
    finally:
        await hass.async_add_executor_job(api.disconnect)

    if not devices:
        # Account authenticated but no Irrisense (WR / WG / WC / WL) devices were returned
        # — probably wrong account for Irrisense, or the user only owns pool cleaners.
        raise NoIrrisenseDevices

    return {
        "title": f"Aiper Irrisense ({data[CONF_USERNAME]})",
        "device_count": len(devices),
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """User-initiated config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except NoIrrisenseDevices:
                errors["base"] = "no_devices"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during Irrisense setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await validate_input(
                    self.hass,
                    {**self._get_reauth_entry().data, **user_input},
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for the Irrisense integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ENABLE_MQTT,
                    default=current.get(CONF_ENABLE_MQTT, True),
                ): bool,
                vol.Optional(
                    CONF_MQTT_DEBUG,
                    default=current.get(CONF_MQTT_DEBUG, False),
                ): bool,
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=current.get(CONF_POLL_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                vol.Optional(
                    CONF_MAP_REFRESH_HOURS,
                    default=current.get(CONF_MAP_REFRESH_HOURS, DEFAULT_MAP_REFRESH_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                vol.Optional(
                    CONF_HISTORY_REFRESH_HOURS,
                    default=current.get(CONF_HISTORY_REFRESH_HOURS, DEFAULT_HISTORY_REFRESH_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                vol.Optional(
                    CONF_REMINDER_REFRESH_HOURS,
                    default=current.get(CONF_REMINDER_REFRESH_HOURS, DEFAULT_REMINDER_REFRESH_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                vol.Optional(
                    CONF_ENABLE_WEATHER,
                    default=current.get(CONF_ENABLE_WEATHER, DEFAULT_ENABLE_WEATHER),
                ): bool,
                vol.Optional(
                    CONF_WEATHER_REFRESH_HOURS,
                    default=current.get(CONF_WEATHER_REFRESH_HOURS, DEFAULT_WEATHER_REFRESH_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                vol.Optional(
                    CONF_ENABLE_EXPERIMENTAL_SENSORS,
                    default=current.get(CONF_ENABLE_EXPERIMENTAL_SENSORS, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class InvalidAuth(HomeAssistantError):
    """Invalid credentials."""


class NoIrrisenseDevices(HomeAssistantError):
    """Account has no Irrisense (WR / WG / WC / WL) devices."""
