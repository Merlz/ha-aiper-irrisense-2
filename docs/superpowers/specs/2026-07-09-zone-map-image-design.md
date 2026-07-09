# Zone Map Image Entity — Design

Date: 2026-07-09
Status: approved (brainstorming + scrutinize pass)
Branch: `feat/zone-map-image` (off upstream/main 992fc20, v0.3.0)

## Goal

One `image` entity per device (`image.<device>_zone_map`) rendering the device's
watering-zone layout as SVG: zone geometry from the cached S3 zone map, the active
zone highlighted, and the sprinkler-head spray position overlaid live while a run
is in progress. No new cloud calls — everything renders from data the coordinator
already holds.

## Non-goals

- No map editing (Tier C, separate roadmap item).
- No camera entity / streaming — image entity only.
- No history rendering (past runs); the image shows current state only.

## Architecture

- New platform module `custom_components/aiper_irrisense/image.py`, added to
  `PLATFORMS` in `__init__.py`.
- `AiperZoneMapImage(IrrisenseEntity, ImageEntity)`, one per device slot,
  following the existing per-device entity pattern.
- Pure renderer module `custom_components/aiper_irrisense/map_render.py`:
  `render_zone_map(regions: list[dict], active: dict | None) -> str` returning an
  SVG document string. No HA imports — unit-testable standalone.

### Data sources (all existing)

| Data | Source | Notes |
|---|---|---|
| Zone geometry | `coordinator.zones_for(sn)` | region dicts: `{id, name, type, flags, points:[{x, y, rotate, valve, waterpress}]}` |
| Active run state | `coordinator.active_zone_state(sn)` | `zone_id`, `progress`, live `x`/`y` (running only), `is_running` |
| Update push | `DataUpdateCoordinator` listener | every MQTT upChan frame triggers `async_set_updated_data` (coordinator.py:496) |

## Rendering

- `content_type = "image/svg+xml"`; `async_image()` renders on demand from the
  current coordinator state (rendering is cheap string building; no cached bytes).
- viewBox = bounding box of all region points plus padding; head origin (0,0)
  always included and drawn as a marker.
- Region types: Area → polygon, Line → polyline, Point → circle at the point
  coordinate. Zone name labels rendered next to each region.
- **All text content XML-escaped** (`xml.sax.saxutils.escape`) — zone names are
  cloud-supplied user data; a literal `&`/`<` must not break the document.
- Active zone (when `is_running` and `zone_id` matches): distinct fill/stroke +
  progress percentage label.
- Live head/spray overlay (marker at live `x`/`y`, line from origin) **only while
  `is_running`** — `active_zone_state()` carries `x`/`y` in the running branch
  only. Idle render = zones + head origin, no spray overlay.
- Empty/missing map (zone-map fetch failed or no regions, the #35 class): render
  a "no map data" placeholder SVG. Entity stays `available` as long as the
  coordinator is healthy — availability tracks the coordinator, not map presence.

## Update mechanics — throttle gate (mandatory)

Naively bumping `image_last_updated` on every coordinator push would create one
recorder state row + one frontend refetch per MQTT frame (~1–2 s cadence during a
run; `realTimeProgress` is the fastest-updating stream). Instead:

- On each coordinator update compute a **render key**:
  `(map regions hash, active zone_id, int(progress), quantised x/y)` with x/y
  quantised to a ~50-unit grid.
- Bump `_attr_image_last_updated` only when the render key changed **and** at
  least 5 s have passed since the previous bump.
- Idle devices produce a stable key → no churn between runs.

## Error handling

- Renderer never raises on malformed region data: skip regions without usable
  points, fall back to the placeholder SVG when nothing is renderable.
- `async_image()` must always return bytes (placeholder on any internal error) —
  an exception there surfaces as a broken image in every dashboard.

## Risk gates (acceptance criteria)

1. **SVG-in-image-entity compatibility is an assumption, not a fact.** HA dev
   docs default `content_type` to `image/jpeg` and don't document SVG; the Kroki
   custom integration is precedent but the Companion app is unverified.
   Acceptance: live render verified on the LB testbed in **both** desktop
   browser and Companion app. If SVG fails → fallback plan B: Pillow PNG
   renderer behind the same entity (swap `map_render.py` internals +
   `content_type`; entity contract unchanged).
2. Dashboard card support (`picture-entity` / tile with an `image.*` entity) to
   be verified live during the dashboard step.

## Testing

- Unit tests for `map_render.py` (no HA needed):
  - fixture regions from real LB maps (`sample_maps/WRX*.json` shapes),
  - XML-escape test (zone named e.g. `A&B <test>`),
  - idle vs. running render (spray overlay presence),
  - empty map → placeholder,
  - malformed region (missing points) skipped without raising.
- Live verification on LB: deploy via dev-combined fork main (HACS Redownload +
  full HA restart — code changes need a full restart), check idle image on both
  clients, then a short physical watering run (requires explicit user consent)
  to verify the live overlay and confirm recorder churn stays bounded (count
  image-entity state rows for the run).

## Rollout

1. Implement on `feat/zone-map-image`, unit tests green.
2. Merge into the `dev-combined` test build → fork main → LB deploy → live gates.
3. After live gates pass: draft PR to Merlz (spec/docs stripped from the PR, as
   with the weather feature).
