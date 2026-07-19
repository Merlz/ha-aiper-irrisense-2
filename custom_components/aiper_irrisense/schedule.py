"""Pure parsing + formatting for Irrisense watering plans (schedules).

Plans live on the device (40 slots, device-assigned ``plan_id``) and are read
over the MQTT command channel: ``WrPlanOverview`` (which slots are in use) and
``WrPlanDetail`` (the full schedule of one plan). This module is stdlib-only —
no Home Assistant imports — so it can be unit-tested in isolation.

Wire shapes are the inner payloads of the ``{"<cmd>": {...}, "res": 0}`` MQTT
frames (the coordinator dispatches by command name and hands us the inner dict).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    """One watering plan (schedule) as reported by ``WrPlanDetail``."""

    plan_id: int
    zone_name: str
    zone_type: int  # map_info.type: 0=Area, 1=Line, 2=Point
    depth: float  # inches (Area/Line zones)
    point_time: int  # minutes (Point zones)
    start_time: str  # "HH:MM"
    repeat_type: int  # 0=Once, 1=Weekly, 2=Biweekly, 3=Triweekly
    weekdays: list[int]  # 0=Sun .. 6=Sat
    enabled: bool
    estimated_time: int  # minutes


_WEEKDAY_NAMES = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")  # 0=Sun .. 6=Sat


def weekday_abbr(weekdays: list[int]) -> str:
    """Human-readable weekday set: ``"Su Mo"`` / ``"Daily"`` / ``"—"``."""
    days = sorted(d for d in weekdays if 0 <= d <= 6)
    if not days:
        return "—"
    if len(days) == 7:
        return "Daily"
    return " ".join(_WEEKDAY_NAMES[d] for d in days)


_ZONE_TYPE_POINT = 2  # map_info.type; mirrors const.REGION_TYPE_POINT


def dose_label(plan: "Plan") -> str:
    """Human dose: minutes for Point zones, millimetres for Line/Area."""
    if plan.zone_type == _ZONE_TYPE_POINT:
        return f"{plan.point_time} min"
    return f"{round(plan.depth * 25.4)} mm"


def parse_overview(payload: dict) -> list[int]:
    """Return the list of in-use plan ids from a ``WrPlanOverview`` payload."""
    return [int(i) for i in payload.get("used_ids", [])]


def parse_plan_detail(payload: dict) -> Plan:
    """Build a :class:`Plan` from a ``WrPlanDetail`` payload."""
    map_info = payload.get("map_info") or {}
    work_ctrl = payload.get("work_ctrl") or {}
    time_ctrl = payload.get("time_ctrl") or {}
    return Plan(
        plan_id=int(payload["plan_id"]),
        zone_name=str(map_info.get("name", "")),
        zone_type=int(map_info.get("type", 0)),
        depth=round(float(work_ctrl.get("depth", 0.0)), 1),
        point_time=int(work_ctrl.get("point_time", 0)),
        start_time=str(time_ctrl.get("start_time", "")),
        repeat_type=int(time_ctrl.get("repeat_type", 0)),
        weekdays=[int(d) for d in time_ctrl.get("weekdays", [])],
        enabled=bool(payload.get("enabled", False)),
        estimated_time=int(payload.get("estimated_time", 0)),
    )
