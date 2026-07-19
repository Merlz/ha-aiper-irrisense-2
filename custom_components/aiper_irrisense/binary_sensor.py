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
                SessionConflictBinarySensor(coordinator, sn),
                LastRunFaultBinarySensor(coordinator, sn),
            ]
        )
    async_add_entities(entities)


class OnlineBinarySensor(IrrisenseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "online"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "online")

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


class SessionConflictBinarySensor(IrrisenseEntity, BinarySensorEntity):
    """On while another client is contending the Aiper account.

    The cloud allows a single session per account; a login elsewhere (the
    Aiper app, another integration) evicts this integration's REST token
    (HTTP 402). This flags that state so it can drive a notification — MQTT
    rides a separate session and keeps working, only REST-backed data goes
    stale until the other client releases. Account-scoped, so every device
    reports the same value.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:account-alert"
    _attr_translation_key = "session_conflict"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "session_conflict")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.api.session_conflict)


class WateringBinarySensor(IrrisenseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:sprinkler"
    _attr_translation_key = "watering"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "watering")

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

    @property
    def is_on(self) -> bool:
        setting = self._slot.get("setting")
        if not isinstance(setting, dict):
            return False
        rain = setting.get("rainSensing")
        weather = setting.get("weatherSensingRain")
        return bool(rain) and bool(weather)


# The two taskStatus outcomes that mean the device could not do its job:
# ``2`` (fault / malfunction) and ``8`` (water shortage — no/insufficient
# supply). The remaining non-completion codes (manual stop, weather skips,
# task overlap, conflicts) are normal operation, not problems.
_TASK_STATUS_PROBLEM = (2, 8)


class LastRunFaultBinarySensor(IrrisenseEntity, BinarySensorEntity):
    """On when the most recent run ended in a device problem.

    Fires on ``taskStatus`` fault (2) or water shortage (8) — e.g. a run that
    aborted with no water delivered — so an automation can react without
    templating the ``last_run_status`` sensor. Normal non-completions (manual
    stop, weather skips) do not trip it.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:sprinkler-variant-alert"
    _attr_translation_key = "last_run_fault"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "last_run_fault")

    @property
    def is_on(self) -> bool | None:
        last = self._latest_history_record
        if last is None:
            return None
        return last.get("taskStatus") in _TASK_STATUS_PROBLEM
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
    _attr_translation_key = "mqtt_connected"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "mqtt_connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.api.is_mqtt_connected()
