# Aiper Irrisense 2 — Home Assistant integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Unofficial Home Assistant integration for the **Aiper Irrisense 2** smart
sprinkler controller. Talks to the same AWS IoT MQTT endpoints as the Aiper
mobile app — no local-network protocol, no extra hardware — and surfaces the
device's zones, sensors, and controls as Home Assistant entities so you can
use them in automations and dashboards.

> **Status:** unofficial, reverse-engineered, not affiliated with Aiper. Use
> at your own risk. Tested on the 2nd-generation Irrisense unit; other
> models may or may not work.

![Single device dashboard](docs/screenshots/single-device.png)


## Features

- **Control:** start / stop watering per zone, pick dose (Area / Line) or
  duration (Point) from a zone-aware select entity.
- **Live telemetry:** active zone, elapsed / remaining seconds, progress %,
  coverage passes, rain-sensing state.
- **Device info:** firmware versions (main / MCU / valve), Wi-Fi RSSI,
  lifetime water delivered / saved, watering-event count.
- **Settings:** toggle rain-sensing, wind-sensing, drainage / pesticide /
  task / water-shortage reminders, and the built-in schedule.
- **Progress-spike filter** in the coordinator suppresses transient
  `0 → 100 → low` blips that the device's `realTimeProgress` stream
  occasionally emits for Area zones.
- **Three example dashboards** under [`examples/`](./examples) (single
  device, dual device, dual device side-by-side).

## Requirements

- Home Assistant **2024.1** or newer
- An Aiper account with at least one Irrisense 2 device already bound to it
  via the Aiper mobile app
- Internet connectivity from your HA host (this is a cloud-polled integration)

## Installation

### HACS (recommended)

1. In HACS → **Integrations** → top-right menu → **Custom repositories**.
2. Add `https://github.com/Merlz/ha-aiper-irrisense-2` with category
   **Integration**.
3. Install **Aiper Irrisense 2** from the HACS list.
4. Restart Home Assistant.
5. **Settings → Devices & services → Add integration → Aiper Irrisense 2**.
6. Enter your Aiper account email + password and choose the region of your account. The config flow will
   auto-discover every device bound to the account.

### Manual

1. Copy `custom_components/aiper_irrisense/` into your HA
   `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings → Devices & services**.

## Entities

Each Irrisense 2 device surfaces the entities below. Entity IDs are derived
from the device name you gave the unit in the Aiper app (lower-snake-cased),
so a device called *IrriSense Garden* will have
`sensor.irrisense_garden_active_zone`, etc.

| Domain          | Name                          | Notes                                                                   |
|-----------------|-------------------------------|-------------------------------------------------------------------------|
| `binary_sensor` | Online                        | Device-reachable flag                                                   |
| `binary_sensor` | Watering                      | `true` during an active run                                             |
| `binary_sensor` | Rain sensing active           | Rain-sense paused the run                                               |
| `button`        | Start watering                | Presses with the current zone + dose/duration                           |
| `button`        | Stop watering                 | Stops the currently-running zone                                        |
| `select`        | Watering zone                 | Options are the zones on the device's zone map                          |
| `select`        | Dose / Duration               | Shape-shifts: `3mm / 6mm / 13mm` for Area/Line, `1min / 5min / 10min` for Point |
| `select`        | Nozzle type                   | Stream / Rotor / etc.                                                   |
| `sensor`        | Active zone                   | Name of the zone currently running. Key attributes: `dose_label`, `duration_seconds`, `duration_pending`, `start_time`, `elapsed_seconds`, `repair_layer`, `progress` |
| `sensor`        | Progress                      | 0–100 %. Spike-filtered                                                 |
| `sensor`        | Elapsed seconds               | Time since the current run started                                      |
| `sensor`        | Run total seconds             | Expected duration of the current run                                    |
| `sensor`        | Coverage passes               | `repair_layer` from the device (0-indexed; humans usually want `+1`)    |
| `sensor`        | WiFi signal                   | dBm                                                                     |
| `sensor`        | Firmware version / MCU / Valve| Three separate sensors                                                  |
| `sensor`        | Total water delivered / saved | Litres (lifetime)                                                       |
| `sensor`        | Watering events               | Lifetime count                                                          |
| `sensor`        | Last watered zone             | Name + timestamp attribute                                              |
| `switch`        | Schedule                      | Enable / disable the device's built-in schedule                         |
| `switch`        | Rain sensing / Wind sensing   | Enable / disable each sensor                                            |
| `switch`        | Drainage / Pesticide / Task / Water shortage reminders | Notification toggles                               |

## Services

The integration registers four services under the `aiper_irrisense` domain.
See [`custom_components/aiper_irrisense/services.yaml`](./custom_components/aiper_irrisense/services.yaml)
for the full field list.

- `aiper_irrisense.start_zone` — start a specific zone with a given dose or duration
- `aiper_irrisense.stop_zone` — stop a specific zone
- `aiper_irrisense.query_work_info` — ask the device to immediately publish its current work snapshot
- `aiper_irrisense.debug_publish` — diagnostic-only raw MQTT publish

## Example dashboards

Three ready-to-paste Lovelace dashboards live under [`examples/`](./examples):

| File                                      | Shape                                                                 |
|-------------------------------------------|-----------------------------------------------------------------------|
| `dashboard-single-device.yaml`            | One dashboard for a single Irrisense 2. Paste into a new dashboard.   |
| `dashboard-dual-device.yaml`              | Two dashboards — one per device — in a single file separated by `---`. Create two dashboards in HA and paste each half into its own. |
| `dashboard-dual-device-alternative.yaml`  | A single dashboard with both devices side by side.                    |

All three depend on these HACS frontend cards:
- [`custom:button-card`](https://github.com/custom-cards/button-card)
- [`custom:stack-in-card`](https://github.com/custom-cards/stack-in-card)
- [`custom:mushroom`](https://github.com/piitaya/lovelace-mushroom) (for `mushroom-chips-card`)

Install those via HACS → Frontend before loading the dashboards.

### Dual Dashboards side-by-side view

Pop up cards when starting a watering zone, with Dosing or Duration based on Zone Type (Area & Line use mm, Point uses Minutes)

![Dual device dashboard (alternative)](docs/screenshots/dual-device-side-by-side.png)

Water usage pills show history

![Dual device dashboard (alternative) history](docs/screenshots/dual-device-side-by-side-history.png)

### 
## Configuration options

Most users only need the username / password from the config flow. Advanced
options available via the integration's **Configure** button:

- **MQTT debug logging** — logs every inbound / outbound MQTT frame at INFO
  level. Off by default. Turn on only for diagnostics; the volume is high.

## Known limitations

### ⚠️ Only one concurrent MQTT session per Aiper account

This is the biggest operational gotcha, so read it before you install.

**The Aiper cloud only allows a single MQTT session per account at a time.**
If you open the Aiper mobile app while Home Assistant is connected, the
cloud will kick Home Assistant off — and vice versa, opening HA's
connection will kick your phone off. You'll see the integration go
offline, reconnect, get kicked again, and so on as long as both clients
keep fighting for the slot.

**Why this happens.** The integration authenticates against the same
Aiper account as the mobile app and receives the same AWS IoT
credentials. AWS IoT enforces one active connection per client ID — when
a second client connects with the same identity, the broker disconnects
the first one. Because Aiper issues one identity per account (not per
device or per client), the phone app and Home Assistant are effectively
two clients trying to share a single seat.

**Practical workaround.** Close the Aiper mobile app fully before
relying on Home Assistant. Opening the app (even briefly, even via a
background push notification) will disconnect HA until HA reconnects —
which then kicks the app. As long as the app stays closed, HA holds the
session.

There is no workaround at the protocol level — this is how Aiper's
cloud is provisioned.

When the integration is kicked off it will attempt to reconnect on its
own. You'll see brief `Online` → `Unavailable` → `Online` cycles in the
state history whenever the other client connects.

**Unverified possibilities** (if anyone tries these, please open an
issue with what you found — the community will benefit):

- *Second account for HA.* In theory you could create a second Aiper
  account for HA and give it access to the same device(s), so the phone
  keeps the primary account and HA gets its own session. **We have not
  verified** whether Aiper supports multi-account device binding, whether
  a share/invite flow exists in the app, or whether zone maps and
  watering settings carry across to the second account or have to be
  re-set up from scratch. Treat this as an open question, not a
  prescription.

### Other limitations

- The device's `progress` field is not consistently populated for Area
  zones; the integration's spike filter helps but cannot invent data
  that isn't on the wire.
- `total_passes` (how many coverage passes a run will make) is **not
  published by the device**. A derivation was attempted and removed as
  unreliable. `Coverage passes` surfaces the live `repair_layer`
  counter only (current pass number, 0-indexed).
- Occasional transient "Could not convert duration" warnings on
  watering-state transitions. Cosmetic; the run itself is unaffected.

## Troubleshooting

- **Device flips between Online and Unavailable every few seconds** — you
  almost certainly have the Aiper mobile app open on the same account.
  See [Only one concurrent MQTT session per Aiper account](#️-only-one-concurrent-mqtt-session-per-aiper-account)
  above; the fix is a dedicated Aiper account for HA, or keeping the
  mobile app fully closed.
- **Device shows offline immediately after adding the integration** — the
  first AWS IoT handshake can take 30–60 s. Give it a minute, then reload
  the integration. If the flapping continues beyond the first minute,
  it's the single-session issue above, not a handshake problem.
- **Start button does nothing** — check that a zone is selected in the
  `Watering zone` select entity. The button presses with whatever zone is
  currently picked.
- **Wrong dose options** — the Dose / Duration select adapts to the
  selected zone's region type. If the options look wrong, the integration's
  zone-type cache may be stale; reload the integration to refresh it.

For anything else, open an issue with:
- HA version
- Integration version (from HACS or `manifest.json`)
- A trace of the failing interaction with **MQTT debug logging** temporarily
  enabled

## Credits

This integration is modelled on [**kmich/ha-aiper**](https://github.com/kmich/ha-aiper)
— the Home Assistant integration for Aiper's pool-cleaning robots. That project
worked out the end-to-end Aiper cloud plumbing first; this Irrisense 2
integration reuses the same overall shape and then swaps out the device-facing
layer for the sprinkler wire format.

Files from `ha-aiper` that were particularly useful as a starting model:

- **`crypto.py`** — The AES-CBC + RSA `encryptKey` envelope Aiper wraps its
  REST calls in. Same scheme across Aiper's product line, so this module
  was adopted with only light rewording.
- **`api.py`** — Login → Cognito identity pool → OpenID token → AWS
  credentials → IoT endpoint discovery → MQTT client bootstrap. The auth
  chain is the same for Irrisense; only the device-listing response shape
  and the MQTT payloads on `upChan` / `downChan` differ.
- **`const.py`** — Regional API endpoint map (`apiamerica` / `apieurope` /
  `apiasia`) and the `aiper/things/{sn}/upChan` / `downChan` topic
  templates. Reused verbatim.
- **`config_flow.py`** — Email / password / region selector and the
  validate-by-logging-in pattern. Irrisense 2's config flow follows the
  same three-field shape.
- **`coordinator.py`** — `DataUpdateCoordinator` wiring for a
  cloud-polled device with MQTT push on top. Structural inspiration; the
  actual state extraction (zones, progress, repair_layer, etc.) is
  Irrisense-specific.

Huge thanks to [@kmich](https://github.com/kmich) — without that project
as a reference, this one would have taken a lot longer to stand up.

## Contributing

Issues and PRs welcome. If you're reverse-engineering additional wire
behaviour, please include packet captures or `mqtt_debug` log excerpts in
the issue so the shape of the new field is verifiable.

## License

[MIT](./LICENSE)

## Disclaimer

This project is not affiliated with, endorsed by, or sponsored by Aiper. All
trademarks are the property of their respective owners. Use at your own
risk; cloud endpoints and wire formats can change at any time without
notice.
