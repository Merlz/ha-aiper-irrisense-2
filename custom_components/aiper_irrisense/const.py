"""Constants for the Aiper Irrisense 2 integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "aiper_irrisense"

# Mobile-app headers observed from Charles capture (iOS v3.3.0)
APP_VERSION: Final = "3.3.0"
APP_OS: Final = "ios"
# Static app key observed in `requestidkey` header across all requests.
REQUEST_ID_KEY: Final = "K6!R]y_]Q!gA,5vy"

# REST base URLs per region. The actual `domain` returned by /login is authoritative
# — these are only used to issue the login call.
API_ENDPOINTS: Final = {
    "us": "https://apiamerica.aiper.com",
    "eu": "https://apieurope.aiper.com",
    "asia": "https://apiasia.aiper.com",
}

# Irrisense serial prefixes. The Aiper app's own device-list classifies
# IrriSense models by the first **two** letters of the serial:
#   * `WR` — IrriSense 2 (original / Irrigo WR / IrriSense WR)
#   * `WG` — IrriSense N2 (big-box retail; Home Depot / Lowe's / Best Buy)
#   * `WC` — IrriSense 2 Chromatic / IrriSense II (further retail variant)
#   * `WL` — IrriSense 2 SE
# The third letter is a production-batch indicator (WRX, WRZ, WGX, WCX, ...);
# all share the same wire protocol. Matching on the 2-letter family keeps us
# in sync with the app's behaviour and avoids a new release every time a new
# batch letter ships.
IRRISENSE_SERIAL_PREFIXES: Final = ("WR", "WG", "WC", "WL")

# Model string as written into the S3 zone-map path.
IRRISENSE_MODEL: Final = "IrriSense_2"

# Config / Options keys
CONF_REGION: Final = "region"
CONF_ENABLE_MQTT: Final = "enable_mqtt"
CONF_MQTT_DEBUG: Final = "mqtt_debug"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_MAP_REFRESH_HOURS: Final = "map_refresh_hours"
CONF_HISTORY_REFRESH_HOURS: Final = "history_refresh_hours"
CONF_REMINDER_REFRESH_HOURS: Final = "reminder_refresh_hours"
CONF_WEATHER_REFRESH_HOURS: Final = "weather_refresh_hours"

DEFAULT_SCAN_INTERVAL: Final = 120  # seconds
DEFAULT_FAST_SCAN_INTERVAL: Final = 5  # seconds during an active watering event
DEFAULT_FAST_WINDOW_SECONDS: Final = 60
DEFAULT_MAP_REFRESH_HOURS: Final = 6
DEFAULT_HISTORY_REFRESH_HOURS: Final = 6
DEFAULT_REMINDER_REFRESH_HOURS: Final = 24
DEFAULT_WEATHER_REFRESH_HOURS: Final = 1

# ---- MQTT topic patterns --------------------------------------------------
# Same AWS IoT infrastructure as the pool cleaner integration — the Irrisense
# takes the non-X9 "downChan"/"upChan" path confirmed from the decompiled APK.
TOPIC_READ: Final = "aiper/things/{sn}/upChan"        # device → cloud / app
TOPIC_WRITE: Final = "aiper/things/{sn}/downChan"     # app → device
TOPIC_CLOUD_REPORT: Final = "aiper/things/{sn}/WR/cloud/report"  # plain-JSON heartbeats
# Shadow (NetStat, OpInfo, AlarmReport only — no watering state)
TOPIC_SHADOW_GET: Final = "$aws/things/{sn}/shadow/get/accepted"
TOPIC_SHADOW_GET_REQUEST: Final = "$aws/things/{sn}/shadow/get"
TOPIC_SHADOW_UPDATE: Final = "$aws/things/{sn}/shadow/update"
TOPIC_SHADOW_UPDATE_ACCEPTED: Final = "$aws/things/{sn}/shadow/update/accepted"
TOPIC_SHADOW_UPDATE_DELTA: Final = "$aws/things/{sn}/shadow/update/delta"
TOPIC_SHADOW_UPDATE_DOCUMENTS: Final = "$aws/things/{sn}/shadow/update/documents"

# ---- MQTT downChan command types ------------------------------------------
# Reverse-engineered from decompiled `com.aiper.device.i` (Irrisense) Android
# module and cross-confirmed via frida capture of the live Android app
# (AWSIotMqttManager.publishString + MqttManager.publishToTopic).
# Wire envelope: {"<cmd>": {...data}}, plain JSON, QoS 1, isEncrypt=false.
# The command name is the top-level key; there is no "type"/"data" wrapper.
CMD_SET_WORK_MODE: Final = "setWorkMode"       # zone start/stop
CMD_WR_CONTROL: Final = "WrControl"            # {cmd:0}=reset, {cmd:1}=start (manual valve)
CMD_WORK_INFO: Final = "workInfo"              # query current work snapshot
CMD_REAL_TIME_PROGRESS: Final = "realTimeProgress"
CMD_REAL_TIME_STATUS: Final = "realtimeStatus"
CMD_GET_WATER_YIELD: Final = "getWaterYield"
CMD_SET_WATER_YIELD: Final = "setWaterYield"
CMD_WR_MAP_BUILD_EXIT: Final = "WrMapBuildExit"

# ---- Region / Zone types (from S3 zone-map JSON) --------------------------
REGION_TYPE_AREA: Final = 0   # polygon → waterYield (mm)
REGION_TYPE_LINE: Final = 1   # path    → waterYield (mm)
REGION_TYPE_POINT: Final = 2  # point   → point_time (MINUTES)

# waterYield presets — wire floats mapped from the app's three-step selector.
# The app displays these as "3 mm / 6 mm / 13 mm" but the on-wire floats are
# 0.1 / 0.25 / 0.5 (confirmed in WrPanelWorkInfoViewModel.startWork$start,
# APK line 1846). Device firmware appears to silently drop off-preset values.
WATER_YIELD_LOW: Final = 0.1     # UI: "3 mm"
WATER_YIELD_MEDIUM: Final = 0.25  # UI: "6 mm"
WATER_YIELD_HIGH: Final = 0.5     # UI: "13 mm"
WATER_YIELD_PRESETS: Final = (WATER_YIELD_LOW, WATER_YIELD_MEDIUM, WATER_YIELD_HIGH)

# point_time presets — in MINUTES (confirmed APK line 1844; UI labels
# "1 min / 5 min / 10 min").
POINT_TIME_LOW: Final = 1
POINT_TIME_MEDIUM: Final = 5
POINT_TIME_HIGH: Final = 10
POINT_TIME_PRESETS: Final = (POINT_TIME_LOW, POINT_TIME_MEDIUM, POINT_TIME_HIGH)

# ---- Dose / Duration labels (shown in the HA Select) ----------------------
# The Aiper app displays the three waterYield presets as "3 mm / 6 mm / 13 mm"
# and the three point_time presets as "1 min / 5 min / 10 min". We keep the
# exact same labels so HA users see identical vocabulary and so automation
# YAML reads naturally ("dose: 6 mm").
#
# These dicts are authoritative for two directions:
#   * label → wire value  (Select → _publish_cmd)
#   * wire value → label  (incoming MQTT snapshot → status banner)
# Neither direction tolerates floats beyond the three presets — the firmware
# silently drops off-preset values (see coordinator._snap_to_preset).
WATER_YIELD_LABELS: Final[dict[float, str]] = {
    WATER_YIELD_LOW:    "3 mm",
    WATER_YIELD_MEDIUM: "6 mm",
    WATER_YIELD_HIGH:   "13 mm",
}
POINT_TIME_LABELS: Final[dict[int, str]] = {
    POINT_TIME_LOW:    "1 min",
    POINT_TIME_MEDIUM: "5 min",
    POINT_TIME_HIGH:   "10 min",
}

# Default selection when the user picks a zone without having touched dose yet.
# Matches the Aiper app default (the lowest preset is pre-selected).
DEFAULT_WATER_YIELD_LABEL: Final = WATER_YIELD_LABELS[WATER_YIELD_LOW]    # "3 mm"
DEFAULT_POINT_TIME_LABEL:  Final = POINT_TIME_LABELS[POINT_TIME_LOW]      # "1 min"


def dose_options_for_region_type(region_type: int | None) -> list[str]:
    """Return the list of human labels the Dose select should show.

    Point zones get the minutes presets ("1 min" / "5 min" / "10 min").
    Area / Line (and the unknown-type fallback) get the mm presets.
    """
    if region_type == REGION_TYPE_POINT:
        return list(POINT_TIME_LABELS.values())
    return list(WATER_YIELD_LABELS.values())


def default_dose_label_for_region_type(region_type: int | None) -> str:
    """Return the label to pre-select when the user picks a zone of this type."""
    if region_type == REGION_TYPE_POINT:
        return DEFAULT_POINT_TIME_LABEL
    return DEFAULT_WATER_YIELD_LABEL


def parse_dose_label(label: str) -> tuple[str, float | int] | None:
    """Reverse-map a Select option back to a wire value.

    Returns a tuple ``(kind, value)`` where ``kind`` is ``"waterYield"`` for
    Area/Line presets and ``"point_time"`` for Point presets. Returns None
    on unknown labels so callers can fall back to the zone-map default.
    """
    for wy, lbl in WATER_YIELD_LABELS.items():
        if lbl == label:
            return ("waterYield", wy)
    for pt, lbl in POINT_TIME_LABELS.items():
        if lbl == label:
            return ("point_time", pt)
    return None


def label_for_water_yield(value: float | None) -> str | None:
    """Nearest-preset label for a waterYield float, or None on garbage input."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Snap to the closest preset so off-preset values (which shouldn't occur
    # but might on very old firmware echoes) still render sensibly.
    closest = min(WATER_YIELD_LABELS, key=lambda p: abs(p - v))
    return WATER_YIELD_LABELS[closest]


def label_for_point_time(value: int | float | None) -> str | None:
    """Nearest-preset label for a point_time (minutes), or None on garbage input."""
    if value is None:
        return None
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    closest = min(POINT_TIME_LABELS, key=lambda p: abs(p - v))
    return POINT_TIME_LABELS[closest]

# ---- Status / Mode maps ---------------------------------------------------
# setWorkMode.status: 1=start, 0=stop
STATUS_RUNNING: Final = 1
STATUS_STOPPED: Final = 0

# setWorkMode.mode: 0=regular watering, 1=pesticide application.
# Source: WrPanelWorkInfoViewModel.startWork (APK :1837) —
#   mode = ((consumeSn is blank) ? 1 : 0) ^ 1
# i.e. no pesticide SN → mode 0; pesticide SN present → mode 1.
# The stop envelope also uses mode=0.
MODE_WATERING: Final = 0
MODE_PESTICIDE: Final = 1

# ---- Nozzle types ---------------------------------------------------------
# Per the iOS app UI, only two nozzle types are exposed:
#   * Standard Nozzle — factory default, general water-source conditions
#   * Jet Nozzle      — for low water pressure / low flow (ships in the box)
# Device-side / UI encoding (what we use internally and send to the user):
#   0 = Standard, 1 = Jet
# Server REST encoding (returned by /wr/getNozzleTypeSetting and accepted by
# /wr/updateNozzleTypeSetting) is 1-indexed and ambiguous on the Standard
# side: a value of 0 _or_ 1 means "Standard", and 2 means "Jet". See the
# decompiled NozzleViewModel (`queryNozzleSettingsByServer` builds an
# IN-array of {0,1} and XORs with 1 → result 0=Standard, 1=Jet; and
# `updateNozzleSettingByServer` sends `value == 1 ? 2 : 1`).
NOZZLE_TYPE_STANDARD: Final = 0
NOZZLE_TYPE_JET: Final = 1

NOZZLE_TYPE_LABELS: Final[dict[int, str]] = {
    NOZZLE_TYPE_STANDARD: "Standard",
    NOZZLE_TYPE_JET: "Jet",
}

# Server GET → device/UI index helper. Used by the select entity to render
# `current_option`.
NOZZLE_SERVER_TO_DEVICE: Final[dict[int, int]] = {
    0: NOZZLE_TYPE_STANDARD,
    1: NOZZLE_TYPE_STANDARD,
    2: NOZZLE_TYPE_JET,
}
