"""Switch platform for the Aiper Irrisense 2.

Exposed switches (per device):
  * One switch per scheduled watering task (enable/disable)
  * Rain sensing (weatherSensingRain + rainSensing)
  * Wind sensing (weatherSensingWind)
  * Drainage / pesticide / task / water-shortage reminders

Schema confirmed from live diagnostics dump.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import IrrisenseCoordinator
from .entity import IrrisenseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IrrisenseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    known_tasks: dict[str, set[int]] = {}

    def _build_new_schedule_switches() -> list[SwitchEntity]:
        new: list[SwitchEntity] = []
        for dev in coordinator.devices:
            sn = dev.get("sn")
            if not sn:
                continue
            tasks = (coordinator.data or {}).get(sn, {}).get("tasks") or []
            task_list = _extract_task_list(tasks)
            seen = known_tasks.setdefault(sn, set())
            for t in task_list:
                tid = t.get("id") or t.get("taskId")
                if tid is None:
                    continue
                try:
                    tid_int = int(tid)
                except (TypeError, ValueError):
                    continue
                if tid_int in seen:
                    continue
                seen.add(tid_int)
                new.append(ScheduleSwitch(coordinator, sn, tid_int, t))
        return new

    # Device-level switches first
    initial: list[SwitchEntity] = []
    for dev in coordinator.devices:
        sn = dev.get("sn")
        if not sn:
            continue
        initial.extend(
            [
                RainSensingSwitch(coordinator, sn),
                WindSensingSwitch(coordinator, sn),
                DrainageReminderSwitch(coordinator, sn),
                PesticideReminderSwitch(coordinator, sn),
                TaskReminderSwitch(coordinator, sn),
                WaterShortageReminderSwitch(coordinator, sn),
            ]
        )
    initial.extend(_build_new_schedule_switches())
    async_add_entities(initial)

    @callback
    def _coordinator_updated() -> None:
        new = _build_new_schedule_switches()
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_coordinator_updated))


def _extract_task_list(tasks_obj: Any) -> list[dict[str, Any]]:
    """Normalize the `tasks` slot to a flat list of dicts.

    Confirmed shape: a plain list of task objects at the top level.
    """
    if isinstance(tasks_obj, list):
        return [t for t in tasks_obj if isinstance(t, dict)]
    if isinstance(tasks_obj, dict):
        for k in ("list", "tasks", "records", "data"):
            val = tasks_obj.get(k)
            if isinstance(val, list):
                return [t for t in val if isinstance(t, dict)]
    return []


# --------------------------------------------------------------------------- #
# Schedule switch
# --------------------------------------------------------------------------- #


_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


class ScheduleSwitch(IrrisenseEntity, SwitchEntity):
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: IrrisenseCoordinator,
        sn: str,
        task_id: int,
        initial: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, sn, f"schedule_{task_id}")
        self._task_id = task_id
        plan_id = initial.get("planId")
        region_id = initial.get("regionId")
        start = initial.get("startTime") or "—"
        zone_name = coordinator.zone_name(sn, int(region_id)) if region_id else None
        label_parts = []
        if plan_id is not None:
            label_parts.append(f"Plan {plan_id}")
        if zone_name:
            label_parts.append(zone_name)
        elif region_id is not None:
            label_parts.append(f"Zone {region_id}")
        label_parts.append(f"@ {start}")
        self._attr_name = f"Schedule: {' · '.join(label_parts)}"

    def _task(self) -> dict[str, Any] | None:
        tasks = self._slot.get("tasks")
        for t in _extract_task_list(tasks):
            tid = t.get("id") or t.get("taskId")
            try:
                if tid is not None and int(tid) == self._task_id:
                    return t
            except (TypeError, ValueError):
                continue
        return None

    @property
    def is_on(self) -> bool:
        t = self._task() or {}
        val = t.get("enabled")
        if val is None:
            val = t.get("isEnabled")
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, str)):
            return str(val).lower() in ("1", "true")
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        t = self._task()
        if not t:
            return None
        repeat_days = t.get("repeatDays")
        repeat_readable = None
        if isinstance(repeat_days, str) and repeat_days:
            try:
                repeat_readable = ",".join(
                    _DAY_NAMES[int(d)] for d in repeat_days.split(",") if d.strip().isdigit()
                )
            except Exception:  # noqa: BLE001
                repeat_readable = None
        return {
            "plan_id": t.get("planId"),
            "region_id": t.get("regionId"),
            "start_time": t.get("startTime"),
            "repeat_days": repeat_readable or repeat_days,
            "estimated_duration_min": t.get("estimatedDuration"),
            "depth_mm": t.get("depth"),
            "duration_min": t.get("duration"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not await self.coordinator.async_set_schedule_enabled(self._sn, self._task(), True):
            raise HomeAssistantError(f"Failed to enable {self._attr_name}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not await self.coordinator.async_set_schedule_enabled(self._sn, self._task(), False):
            raise HomeAssistantError(f"Failed to disable {self._attr_name}")


# --------------------------------------------------------------------------- #
# Weather / reminder switches (mirror `getWateringSettingV2` + `getReminderSetting`)
# --------------------------------------------------------------------------- #


class _SettingSwitch(IrrisenseEntity, SwitchEntity):
    """Base for switches backed by a single key in the `setting` or `reminder` slot."""

    _slot_name: str = "setting"
    _setting_key: str = ""
    # How to write. For `setting` keys, we call updateWateringSetting with
    # {key: 1|0}. For `reminder` keys, we call the dedicated endpoint.
    _writer: str = "setting"

    @property
    def is_on(self) -> bool:
        slot = self._slot.get(self._slot_name)
        if isinstance(slot, dict):
            val = slot.get(self._setting_key)
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, str)):
                return str(val).lower() in ("1", "true")
        return False

    async def _write(self, value: int) -> None:
        ok = False
        if self._writer == "setting":
            ok = await self.coordinator.async_set_watering_setting(
                self._sn, {self._setting_key: value}
            )
        elif self._writer == "reminder":
            ok = await self.coordinator.async_set_reminder(
                self._sn, self._setting_key, bool(value)
            )
        if not ok:
            raise HomeAssistantError(
                f"Failed to update {self.name or self._setting_key}"
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(0)


class RainSensingSwitch(_SettingSwitch):
    _slot_name = "setting"
    _setting_key = "weatherSensingRain"
    _attr_icon = "mdi:weather-pouring"
    _attr_translation_key = "rain_sensing_switch"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "rain_sensing_switch")


class WindSensingSwitch(_SettingSwitch):
    _slot_name = "setting"
    _setting_key = "weatherSensingWind"
    _attr_icon = "mdi:weather-windy"
    _attr_translation_key = "wind_sensing_switch"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "wind_sensing_switch")


class DrainageReminderSwitch(_SettingSwitch):
    _slot_name = "reminder"
    _setting_key = "drainageReminder"
    _writer = "reminder"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-pump-off"
    _attr_translation_key = "reminder_drainage"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "reminder_drainage")


class PesticideReminderSwitch(_SettingSwitch):
    _slot_name = "reminder"
    _setting_key = "pesticideReminder"
    _writer = "reminder"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bottle-tonic"
    _attr_translation_key = "reminder_pesticide"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "reminder_pesticide")


class TaskReminderSwitch(_SettingSwitch):
    _slot_name = "reminder"
    _setting_key = "taskReminder"
    _writer = "reminder"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bell-alert"
    _attr_translation_key = "reminder_task"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "reminder_task")


class WaterShortageReminderSwitch(_SettingSwitch):
    _slot_name = "reminder"
    _setting_key = "waterShortageReminder"
    _writer = "reminder"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-alert"
    _attr_translation_key = "reminder_water_shortage"

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str) -> None:
        super().__init__(coordinator, sn, "reminder_water_shortage")
