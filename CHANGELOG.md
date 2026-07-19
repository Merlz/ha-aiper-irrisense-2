# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] — More entities + internationalisation

### Added

- **Free dose / duration Number entities + current-dose sensor** (#52 by
  @Patch76). Set any in-range depth (3–23 mm) or point time (1–150 min) beyond
  the three Dose-select presets; `sensor.*_current_dose` mirrors what the next
  Start will use.
- **Rain / wind skip-threshold Number entities** (#46 by @Patch76). Adjust the
  `rainAmount` / `windSpeed` thresholds above which a scheduled run is skipped;
  published in mm / km/h and converted to the device's wire units.
- **Read-only schedule (plan) sensor** (#47 by @Patch76). `sensor.*_schedules`
  surfaces the device-resident watering plans (queried over MQTT, since REST
  doesn't expose them). Queried on a 30-minute cadence.
- **MQTT-connected diagnostic binary sensor** (#48 by @Patch76) — shows whether
  the AWS IoT command link is up, distinct from the REST-layer `online` sensor.
- **Live spray-distance sensor** (#50 by @Patch76) — throw reach in metres,
  derived from the realTimeProgress spray target.
- **Six locale translations** — German, French, Spanish, Italian, Portuguese,
  Dutch (#51 by @Patch76). Entity names are now driven by translation keys, so
  they localise to the HA UI language.

### Notes

- On a non-English install, entity IDs are minted from the localized names, so
  the example dashboards (keyed on English IDs) may need adjusting — see the
  Troubleshooting section.

## [0.4.0] — Features, resilience, security hardening

### Added

- **Zone-map image entity** — live SVG of the zone map with the active zone and
  sprinkler position overlaid (#44 by @Patch76).
- **Per-run history sensors** — last-run water used / saved, duration, and
  outcome status; plus remaining-time and head-angle live sensors (#42 by
  @Patch76).
- **Session-conflict & last-run-fault binary sensors** for diagnostics (#42/#43
  by @Patch76).
- **Weather entity (opt-in, default OFF)** backed by Aiper's WeatherKit
  endpoint (#32 by @Patch76). Enabling it sends your Home Assistant home
  latitude/longitude to Aiper's cloud on each refresh, so it is gated behind an
  **Enable weather** option and documented in the README privacy note.
- **Experimental pesticide-usage / skip-history sensors (opt-in, default OFF)**
  behind an **Enable experimental sensors** option (#40 by @Patch76). Request
  paths are verified; record shapes are best-effort pending real-hardware data.
- **Cloud protocol overview** doc (#49 by @Patch76).

### Changed

- **Serial matching broadened to the 2-letter Irrisense family** (`WR`, `WG`,
  `WC`, `WL`) so WCX / WRZ / future batch letters are accepted (#25 by
  @Patch76; fixes #20, #45).
- **Writable switches now send full payloads** so rain/wind-sensing and
  schedule toggles actually apply instead of reverting (#28 by @Patch76;
  duplicate #37 by @cedbossneo closed; fixes #27).
- **Setup timeouts raised** (login 30 s, discovery 20 s) so a slow cloud login
  no longer trips a spurious `ConfigEntryNotReady` (#38 by @cedbossneo).
- **Zone-map fetch retries after a short backoff** instead of the full refresh
  interval (#36 by @Patch76).
- **Device list persisted across restarts** so an intermittent cloud 402
  doesn't drop all entities; the cache now lives under HA's `.storage`, loads
  off the event loop, and is rebuilt from each successful response so removed
  devices disappear (#33 by @tatutsa888, reworked on merge).
- Unused imports removed; stale `water_pressure_kpa` dashboard chip dropped;
  integration icon added (#24, #26, #8 by @Patch76 / @CtznSniiips).

### Security

- **`debug_publish` service locked down** — only registered when MQTT debug
  logging is enabled, requires an admin caller, and the topic must be scoped to
  the named device (`aiper/things/<sn>/…`).
- **Token-bearing response bodies scrubbed** from exception messages and the
  Cognito warning log (they could reach HA logs and the reauth UI).
- **Zone-map URL fetch restricted** to `https://` on an allowlisted host
  (`.aiper.com` / `.amazonaws.com`) to prevent a cloud-controlled fetch from
  being aimed elsewhere.
- **Log levels aligned with diagnostics redaction** — the Cognito identity id
  and full command frames no longer log at INFO unless MQTT debug is on.

## [0.3.0] — Bug fixes, point-zone watchdog, robust setup

### Added

- **Point-zone overrun watchdog** (#6, #18 by @Patch76). HA-side stop at
  `point_time + 30s` grace when V3.8.7+ firmware mistracks point-zone
  duration. Auto-cancels on a clean device stop or a manual Stop.
- **Skip disabled devices** (#10, #14 by @Patch76). Devices disabled in HA's
  device registry are excluded from setup, MQTT subscribe, and coordinator
  refresh.
- **Integration icon** (#8 by @CtznSniiips).

### Changed

- **Bounded setup latency** (#11, #19 by @Patch76). Login and device discovery
  are wrapped in 15s timeouts that raise `ConfigEntryNotReady` /
  `ConfigEntryAuthFailed` for proper Home Assistant retry, and the MQTT
  connect moved to an entry-bound background task so a slow AWS IoT handshake
  can't push setup past HA's 60s bootstrap window.
- **Water totals now reported in gallons** (#22 by @tiloman). The backend
  reports gallons; the sensors were mislabelled as liters and Home Assistant
  converts for metric users. **Note:** existing history for the water-total
  sensors will shift to the corrected unit.

### Fixed

- **`binary_sensor.*_watering` stuck `off`** during active runs (#4, #15 by
  @Patch76). Now reads `is_running` from the coordinator's `active_zone_state()`
  rather than walking a non-existent nested MQTT `data` wrapper.
- **`water_pressure` permanently `unknown`** (#5, #16 by @Patch76). Removed the
  unreliable sensor and the `water_pressure_kpa` attribute — V3.8.7 firmware
  doesn't publish `waterpress` on progress frames and the fallback scan latched
  stale values from unrelated shadow frames.

## [0.2.2] — US region hostname fix + broader WGX coverage

### Fixed

- **US region login failed** with `Name does not resolve` for
  `apius.aiper.com`. Corrected hostname to `apiamerica.aiper.com` — the
  Aiper cloud's actual US REST endpoint (the EU and Asia endpoints were
  already correct and are unchanged).

### Changed

- Broadened the WGX serial-prefix handling started in 0.2.1 so the rest
  of the integration's user-facing surface no longer says "WRX only":
  - `IRRISENSE_SERIAL_PREFIXES` constant updated to `("WRX", "WGX")`.
  - Config-flow description and `no_devices` error message (English +
    translation) now reference both prefixes.
  - "No devices found" warning log and `NoIrrisenseDevices` docstring
    updated to match.

  `WRX` is the original / online-store SKU; `WGX` is the big-box-retail
  variant (e.g. Costco). Both speak the same wire protocol.

## [0.2.1] — WGX serial-prefix support

### Fixed

- Device-list filter rejected Irrisense units with a `WGX` serial
  prefix (sold via big-box retail) because it only matched `WRX`. The
  filter in `api.get_devices` now accepts both prefixes.
  (Thanks to [@n0k0m3](https://github.com/n0k0m3) — PR #1.)

## [0.2.0] — Initial public release

First public release. The integration has been iterated on privately; this
snapshot is the cleaned-up baseline from which future changes will be tracked.

### Features

- Cloud-polled control of Aiper Irrisense 2 devices via MQTT over AWS IoT.
- Per-device entities: active zone, progress %, coverage passes, elapsed /
  remaining seconds, water pressure, rain-sensing state, firmware versions,
  Wi-Fi signal, lifetime water totals.
- Start / stop watering buttons plus a shape-shifting Dose / Duration select
  that adapts to the currently-selected zone's region type (Area / Line /
  Point).
- Progress-spike filter in the coordinator to suppress transient 0→100→low
  blips from the device's `realTimeProgress` stream.
- Three example Lovelace dashboards (single-device, dual-device, and a
  side-by-side alternative) under `examples/`.
