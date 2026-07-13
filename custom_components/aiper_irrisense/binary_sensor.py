"""Binary sensor platform for the Aiper Irrisense 2."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[BinarySensorEntity] = []
    for dev in coordinator.devices:
        sn = dev.get("sn")
        if not sn:
            continue
        entities.extend(
            [
                OnlineBinarySensor(coordinator, sn),
                MqttConnectedBinarySensor(coordinator, sn),
                WateringBinarySensor(coordinator, sn),
                RainSensingBinarySensor(coordinator, sn),
            ]
        )
    async_add_entities(entities)


class OnlineBinarySensor(IrrisenseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "online"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "online")
        self._attr_name = "Online"

    @property
    def is_on(self) -> bool:
        # Confirmed from diagnostics: equipment.online == 1 when the cloud
        # considers the device reachable.
        val = self._device_dict.get("online")
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, str)):
            return str(val).lower() in ("1", "true", "online")
        return False


class WateringBinarySensor(IrrisenseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:sprinkler"
    _attr_translation_key = "watering"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "watering")
        self._attr_name = "Watering"

    @property
    def is_on(self) -> bool:
        # Delegate to active_zone_state — its freshest-frame pick across
        # _ACTIVE_SOURCES avoids stale-slot divergence (issue #4).
        state = self.coordinator.active_zone_state(self._sn)
        return bool(state and state.get("is_running"))


class RainSensingBinarySensor(IrrisenseEntity, BinarySensorEntity):
    """Reflects whether the device is actively using rain sensing.

    Backed by `setting.rainSensing` + `setting.weatherSensingRain` — both must
    be 1 for the device to defer watering on rain. We expose the combined state
    here; the individual toggles live in the switch platform.
    """

    _attr_icon = "mdi:weather-pouring"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "rain_sensing"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "rain_sensing")
        self._attr_name = "Rain sensing active"

    @property
    def is_on(self) -> bool:
        setting = self._slot.get("setting")
        if not isinstance(setting, dict):
            return False
        rain = setting.get("rainSensing")
        weather = setting.get("weatherSensingRain")
        return bool(rain) and bool(weather)


class MqttConnectedBinarySensor(IrrisenseEntity, BinarySensorEntity):
    """Whether the integration's AWS IoT MQTT link is up.

    Distinct from `online` / `session_conflict`, which reflect the REST/cloud
    layer. Commands (setWorkMode, plan queries) ride MQTT, so this shows when
    they can actually be delivered. AWS IoT allows one connection per Cognito
    identity (last connect wins), so opening the iOS Aiper app evicts this
    link until auto-recovery reconnects.

    Reflects the client's connected belief: it can briefly read "on" during a
    half-dead state where the socket is up but publishes time out.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "MQTT connected"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "mqtt_connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.api.is_mqtt_connected()
