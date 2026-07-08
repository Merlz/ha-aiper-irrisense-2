"""Shared entity base for Aiper Irrisense 2 platforms."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IrrisenseCoordinator


class IrrisenseEntity(CoordinatorEntity[IrrisenseCoordinator]):
    """Base class: one entity per (device SN, logical function)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IrrisenseCoordinator, sn: str, key: str) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._attr_unique_id = f"{sn}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._device_dict
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            manufacturer="Aiper",
            model=dev.get("modelName") or "Irrisense 2",
            name=dev.get("name") or f"Irrisense {self._sn}",
            sw_version=dev.get("firmwareVersion") or dev.get("version"),
            serial_number=self._sn,
        )

    # ----- Data accessors ------------------------------------------------

    @property
    def _device_dict(self) -> dict[str, Any]:
        slot = self.coordinator.data.get(self._sn) if self.coordinator.data else None
        if isinstance(slot, dict):
            equip = slot.get("equipment")
            if isinstance(equip, dict):
                return equip
        # Fallback to the api cache
        return self.coordinator.api._devices.get(self._sn, {})  # noqa: SLF001

    @property
    def _slot(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._sn, {})

    @property
    def _wr_info(self) -> dict[str, Any]:
        wi = self._slot.get("wr_info")
        return wi if isinstance(wi, dict) else {}

    @property
    def _mqtt(self) -> dict[str, Any]:
        m = self._slot.get("mqtt")
        return m if isinstance(m, dict) else {}

    @property
    def _latest_history_record(self) -> dict[str, Any] | None:
        """Most recent watering-history record, or None.

        The backend returns the record list under a version-dependent key
        (``list`` / ``records`` / ``data``); this centralises the lookup the
        history-backed sensors share.
        """
        history = self._slot.get("history")
        if not isinstance(history, dict):
            return None
        items = history.get("list") or history.get("records") or history.get("data") or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)
