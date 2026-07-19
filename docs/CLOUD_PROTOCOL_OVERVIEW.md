# Aiper Irrisense 2 – Cloud Protocol Overview

An overview of the cloud protocol this integration speaks to, for anyone who
wants to understand how the Irrisense 2 talks to its backend or build their
own tooling against it.

> **Scope and disclaimer.** This is an *overview*, not a formal specification.
> It is unofficial and not affiliated with or endorsed by Aiper. Everything
> here is derived from observing the traffic between an Irrisense 2 device and
> its backend and from this integration's own behaviour. Field names, message
> shapes, and endpoints can change between app and firmware versions – treat
> this as a map, and verify anything load-bearing against your own device
> before relying on it. No credentials, keys, or account identifiers are
> published here; you supply your own by testing.

## Architecture

This integration drives the device entirely over the cloud (the device also
has a local BLE interface, out of scope here – see the end). Two backend
surfaces are involved:

```
  App / HA integration  ──HTTPS (REST)──▶  Regional REST cloud
          │                                (auth, device list,
          │                                 settings, telemetry)
          │
          └──────────MQTT over TLS────────▶  AWS IoT Core
                                             (live commands + telemetry,
                                              device shadow)
                                                     │
                                                     ▼
                                                  Device
```

- **REST** handles account login, device enumeration, persistent settings, and
  point-in-time telemetry snapshots.
- **AWS IoT MQTT** carries the live command/response channel and streaming
  telemetry. The device is an AWS IoT "thing"; the app and this integration
  are just other MQTT clients on the same account.

## REST layer

REST calls go to a regional host:

| Region | Host                        |
|--------|-----------------------------|
| US     | `https://apiamerica.aiper.com` |
| EU     | `https://apieurope.aiper.com`  |
| Asia   | `https://apiasia.aiper.com`    |

**Encrypted envelope.** Most REST request and response bodies are not sent as
plain JSON. The payload is wrapped in a hybrid scheme:

- the JSON body is encrypted with **AES-CBC** and carried in a `data` field;
- the AES key and IV are themselves **RSA-encrypted** (PKCS#1 v1.5) and carried
  in a request header.

So to talk to the REST API you decrypt the `data` field after the round-trip.
The RSA public key and the static app-identifier header are per-app constants,
not per-user secrets, but they are not reproduced here – extract them from your
own client if you need them.

**Plans are not REST-managed.** Watering plans (schedules) cannot be *created*
through REST. Attempting to add a plan without a device-assigned id is rejected
by the backend. Plan lifecycle lives on the MQTT side (see below).

## MQTT layer

The transport is AWS IoT Core over MQTT/TLS. Relevant topics, keyed by the
device serial `{sn}`:

| Topic                              | Direction        | Purpose                          |
|------------------------------------|------------------|----------------------------------|
| `aiper/things/{sn}/downChan`       | client → device  | commands                         |
| `aiper/things/{sn}/upChan`         | device → client  | command responses + telemetry    |
| `aiper/things/{sn}/WR/cloud/report`| device → cloud   | plain-JSON heartbeats            |
| `$aws/things/{sn}/shadow/...`      | both             | standard AWS IoT device shadow   |

**Command envelope.** A command is a single-key JSON object whose key is the
command name and whose value is the command's argument object:

```json
{ "setWorkMode": { ... } }
```

Responses arrive on `upChan` in the same single-key shape, echoing the command
name with a result body:

```json
{ "setWorkMode": { ... } }
```

Responses have also been observed to carry a top-level `res` result field
(`0` = success). This integration does not currently depend on it.

### Command families implemented by this integration

| Command            | Purpose                                             |
|--------------------|-----------------------------------------------------|
| `setWorkMode`      | start / stop watering for a zone                    |
| `WrControl`        | manual valve control (reset / start)                |
| `workInfo`         | query the current work snapshot                     |
| `realTimeProgress` | streaming watering progress                         |
| `realtimeStatus`   | streaming device status                             |
| `getWaterYield` / `setWaterYield` | read / write water-yield settings    |
| `WrMapBuildExit`   | map-build control                                   |

### Plan / schedule channel (observed, not yet implemented here)

Watering plans are managed over MQTT through a separate command family. These
commands are **not** part of this integration's shipped surface yet; they are
documented here as observed protocol so contributors and other developers know
where plan management lives:

| Command             | Purpose                                  |
|---------------------|------------------------------------------|
| `WrPlanOverview`    | list the plan ids currently on the device |
| `WrPlanDetail`      | read one plan by id                       |
| `WrPlanBatchEdit`   | enable / disable / edit plans             |
| `WrPlanConfig`      | plan-level configuration                  |
| `WrPlanBatchDelete` | delete plans                              |

They use the same single-key envelope on `downChan` and are answered on
`upChan`.

## Behaviour and constraints

- **Plans are device-resident.** The device owns a fixed set of plan slots and
  assigns the plan id itself. A client reads and edits existing plans; it does
  not mint ids. This is why REST cannot create a plan.
- **One MQTT connection per account identity.** AWS IoT ties the client id to
  the account's identity, and the policy is exact-match: only one client can
  hold that connection at a time. When the mobile app connects, it evicts any
  other client (including this integration) and vice versa – **last connect
  wins**. There is no way to run the app and a second MQTT client side by side.
- **Eventual consistency.** After a command, device state updates asynchronously
  over `upChan` / the shadow. Poll or wait for the response rather than assuming
  an immediate effect.

## Not covered here: BLE

The Irrisense 2 also exposes a local Bluetooth Low Energy interface. That is a
separate transport with its own protocol and is out of scope for this
cloud-focused overview.
