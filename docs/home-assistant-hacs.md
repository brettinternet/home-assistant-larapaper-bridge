# Home Assistant HACS Integration — retained architecture

Goal: install one Home Assistant custom integration through HACS, provision one synthetic Larapaper device, and expose its current screen as a native Home Assistant camera while Home Assistant reads never advance Larapaper's playlist.

This document replaces the external Bun bridge and stock Generic Camera architecture as the primary Home Assistant design. The existing `scripts/larapaper-bridge.ts` and `scripts/larapaper-bridge.test.ts` remain protocol and behavior references from the superseded bridge work; they are not HACS V1 implementation targets.

## Product decision and migration boundary

The primary product is now a HACS custom integration, not a HACS dashboard/plugin. A HACS dashboard plugin is frontend JavaScript and cannot own Larapaper authentication, device-cadence polling, image normalization, or a cache. The implementation target is a Python integration under `custom_components/larapaper_bridge/` with a config flow, a native `CameraEntity`, an independent display scheduler, and Home Assistant diagnostics.

The external Bun/ImageMagick bridge is deferred standalone functionality for non-Home-Assistant clients or network topologies where Home Assistant cannot reach Larapaper. It is not part of HACS V1. The former external-bridge backlog is superseded and deleted by this design; no Dockerfile, Compose deployment, public `/image` route, public `/health` route, bridge Basic auth, or Generic Camera configuration is required for HACS V1.

V1 is implemented and released from a dedicated public repository, `brettinternet/home-assistant-larapaper-bridge`, rather than this TRMNL recipes repository. This avoids coupling recipe and integration release history, README content, issues, and compatibility testing. HACS requires one managed integration per repository and all runtime files under `custom_components/<domain>/`; HA-HACS-01 creates or reserves the repository, and HA-HACS-05 configures and publishes it.

## Why a custom integration is required

Larapaper requests require custom `ID` and `Access-Token` headers. A native Home Assistant integration can send those headers from the Home Assistant process, while a camera read remains cache-only. Home Assistant's authenticated camera proxy serves the entity; no custom unauthenticated image endpoint is needed. Larapaper may return BMP, so the integration must validate image bytes and publish PNG through `CameraEntity`.

The integration must keep the side-effect boundary explicit:

- `GET /api/setup` is provisioning and sends `ID` only.
- `GET /api/display` is the only display-cycle call and sends `ID` plus `Access-Token`.
- `CameraEntity.async_camera_image()` reads only the immutable in-memory cache.
- Image-host requests are made only by the display-cycle image pipeline.
- No Home Assistant dashboard refresh, camera proxy request, or diagnostics read may call `/api/display`.

Official HACS and Home Assistant references:

- [HACS general publishing requirements](https://www.hacs.xyz/docs/publish/start/)
- [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/)
- [HACS dashboard/plugin requirements](https://www.hacs.xyz/docs/publish/plugin/)
- [Home Assistant custom integration file structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [Home Assistant config flows](https://developers.home-assistant.io/docs/core/integration/config_flow/)
- [Home Assistant Camera entity API](https://developers.home-assistant.io/docs/core/entity/camera/)
- [Home Assistant data fetching and coordinators](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Home Assistant integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Home Assistant diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics)

Pinned Larapaper evidence is at commit [`bc114028354d2948fe868f938ed8d41de779b7ac`](https://github.com/usetrmnl/larapaper/tree/bc114028354d2948fe868f938ed8d41de779b7ac):

- [`SetupController.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Http/Controllers/Api/Firmware/SetupController.php)
- [`ResolveDeviceByMacAddress.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Actions/Api/ResolveDeviceByMacAddress.php)
- [`DisplayController.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Http/Controllers/Api/Firmware/DisplayController.php)
- [`RunDeviceDisplayCycle.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Actions/Api/RunDeviceDisplayCycle.php)
- [`DeviceImageResolver.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Services/DeviceImageResolver.php)
- [`config/filesystems.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/config/filesystems.php)

Pinned Home Assistant evidence is at commit [`798888125a13838bce8a15b7b5f81fd9738334d5`](https://github.com/home-assistant/core/tree/798888125a13838bce8a15b7b5f81fd9738334d5):

- [`config_flow.py`](https://github.com/home-assistant/core/blob/798888125a13838bce8a15b7b5f81fd9738334d5/homeassistant/components/generic/config_flow.py)
- [`camera.py`](https://github.com/home-assistant/core/blob/798888125a13838bce8a15b7b5f81fd9738334d5/homeassistant/components/generic/camera.py)

## HACS package and runtime shape

```text
custom_components/
  larapaper_bridge/
    __init__.py
    manifest.json
    config_flow.py
    client.py
    provisioning.py
    scheduler.py
    image.py
    camera.py
    diagnostics.py
    const.py
    strings.json
    translations/
      en.json
    brand/
      icon.png

hacs.json
README.md
```

The integration manifest declares the immutable domain `larapaper_bridge`, `name`, `version`, `documentation`, `issue_tracker`, `codeowners`, `config_flow: true`, `integration_type: "device"`, `iot_class: "local_polling"`, and `single_config_entry: true`. `manifest.json` carries Python `requirements` only when a package is absent from every supported Home Assistant Core version.

Root `hacs.json` contains exactly the HACS display `name` and `"homeassistant": "2026.7.0"` for V1. The minimum version does not belong in `manifest.json`; do not set `content_in_root`, `zip_release`, `filename`, `persistent_directory`, a minimum HACS version, or a HACS `requirements` key without a demonstrated need.

The supported V1 deployment is one self-hosted device reachable from Home Assistant's local network. Change the IoT classification only if a cloud deployment becomes supported. Validate every manifest and `hacs.json` key before release.

Home Assistant `2026.7.0` includes Pillow `12.2.0`, so the integration must not duplicate Pillow in `manifest.json`; the exact minimum-version CI lane verifies import and BMP-to-PNG behavior. The required original, provenance-recorded 256×256 brand asset lives at `custom_components/larapaper_bridge/brand/icon.png`, matching current Home Assistant and HACS standard-layout validation.

The release repository must be public on GitHub, have the decided slug `brettinternet/home-assistant-larapaper-bridge`, a concise description, searchable topics, a README, one `custom_components/larapaper_bridge` integration, and the integration-local brand asset.

The integration runs inside Home Assistant and reaches Larapaper plus returned image hosts directly. Home Assistant's own authentication protects the camera proxy. Larapaper API credentials remain private integration state; they never appear in entity state, diagnostics, logs, image-host headers, or dashboard URLs.

## Architecture

```mermaid
flowchart LR
  UI[Home Assistant dashboard] -->|camera proxy| C[Native CameraEntity]
  C -->|cache-only bytes| M[Immutable in-memory last-good PNG]
  F[Config flow] --> S[Private atomic HA Store]
  S --> P[Provisioning task]
  P -->|GET /api/setup: ID| L[Larapaper API]
  P --> D[Independent display scheduler]
  D -->|GET /api/display: ID + Access-Token| L
  L -->|image_url + refresh_rate| I[Bounded image pipeline]
  I -->|validated HTTP(S), no Larapaper headers| X[Returned image host]
  I --> M
  D --> H[Diagnostics and availability]
```

Only the independent scheduler calls `/api/display`. `CameraEntity.async_camera_image()` never performs network I/O. Home Assistant reads cannot advance telemetry or the playlist.

## Settled operations

### Configuration and config flow

Configuration is entered through Home Assistant's config flow, not environment variables:

- Required `Larapaper base URL`: trimmed absolute HTTP(S), no credentials, query, or fragment; canonicalize a trailing slash while preserving an allowed pathname prefix before appending `api/setup` and `api/display`.
- Optional `image base URL`: blank means unset; otherwise require a trimmed absolute HTTP(S) origin with no credentials, non-root path, query, or fragment. Mechanically copy only its normalized scheme, hostname, and effective port onto the source image path and query.
- Optional device MAC: blank means generate one with a CSPRNG; canonicalize six colon-delimited hexadecimal octets to uppercase.
- Minimum poll seconds: default `60`, finite and positive.
- Maximum stale seconds: default `3600`, positive integer.
- Maximum image bytes: default `10485760`, positive integer.

The config flow validates syntax, loads the fixed domain-wide Store key `larapaper_bridge` before selecting a MAC, and reuses any pending or complete persisted MAC. It awaits private atomic pending-state `async_save` before `async_create_entry`. If the flow is interrupted after Store save but before entry creation, the next flow reuses that identity; a conflicting supplied MAC aborts without mutation. Provisioning runs as an integration-owned background task so the flow does not remain open across the +5/+10/+20/+40/+60 retry schedule. The camera remains unavailable until complete credentials and a fresh image exist. A setup 404 maps to `setup_auto_assign_disabled` and is exposed through safe diagnostics with actionable `assign_new_devices` guidance; the integration never assigns a model or playlist.

Only one config entry/device is supported in V1. Multi-device entries, discovery, automated model/playlist assignment, and on-demand display services are non-goals.

### Persistent identity and provisioning

The HA Store payload is exactly one of:

```json
{"version":1,"mac":"AA:BB:CC:DD:EE:FF"}
```

or:

```json
{"version":1,"mac":"AA:BB:CC:DD:EE:FF","api_key":"...","friendly_id":"..."}
```

The Store constructor's version/minor-version fields govern the Home Assistant storage envelope; the payload `version` field is the independent Larapaper identity-schema version and both are validated separately. Construct the Store with private, atomic writes. The integration creates its storage parent on demand; image bytes are never persisted. Malformed or unsupported envelope/payload state is a non-retrying config-entry setup error, or an unrecoverable config-flow error before entry creation; leave the state intact and never delete or silently reprovision.

Persist pending state before the first setup request. `GET /api/setup` sends `ID` exactly once, never sends `Access-Token`, follows no redirects, and applies a 10-second operation timeout that also bounds response-body consumption. Accept only 2xx JSON with nonempty documented credentials. Persist complete state only after successful setup. Reuse complete state without setup. Never delete state, silently reprovision, or generate a replacement MAC after a failed/restarted setup.

Pending setup failures retry at +5, +10, +20, +40, then +60 seconds repeatedly while the config entry remains loaded. A pending state survives Home Assistant restart and resumes with the same MAC. A complete state survives restart and skips setup.

### Larapaper display cycle

After complete provisioning, start one independent display scheduler for the config entry. Start the first display attempt immediately. Use the HA event loop's monotonic clock for scheduling. When a display attempt is classified as accepted, invalid, or failed, capture that settlement instant and set the next display deadline to settlement plus the selected effective interval; expose a separate UTC projection for diagnostics. Image retries use the captured absolute monotonic deadline. Each accepted response requires a finite positive `refresh_rate`; the effective interval is `max(refresh_rate, minimum poll seconds)`.

`GET /api/display` sends `ID` and `Access-Token` exactly once per scheduled cycle. Refuse redirects and apply a 10-second operation timeout including response-body consumption.

- Display failure has no fast retry because the request's side effect is ambiguous. Schedule the next attempt from the failure settlement instant using the previous valid effective interval, or the configured minimum before a first valid response.
- Invalid or missing `refresh_rate` rejects the entire result, preserves the cache, and uses the prior effective interval or configured minimum.
- Null, empty, or missing `image_url` records `image_url_missing`, preserves the cache, and schedules at the valid effective interval without image fetch or image retry.
- Non-string non-null `image_url` rejects the result as `invalid_display_response`.
- Image recovery never recalls `/api/display`.

### Lifecycle epoch and display-cycle generation

Use two tokens; never use one generation counter for all work.

`lifecycle_epoch` represents the lifetime of the loaded config entry. Increment it only on unload/invalidation. It guards provisioning, credential/state writes, scheduler state, image work, diagnostics, timers, and metrics.

`cycle_generation` represents one display cycle. At each display deadline, increment it on the Home Assistant event loop first, then cancel/abandon old image work with no intervening await, then start the new display call. It guards only display/image/cache/retry/health side effects for that cycle.

Every asynchronous operation captures the applicable token:

```python
OperationToken(
    lifecycle_epoch=current_lifecycle_epoch,
    cycle_generation=current_cycle_generation_or_none,
)
```

Setup/provisioning operations are lifecycle-scoped and do not become invalid merely because a display cycle advances. Image and display operations are scoped to both tokens. After every await, marshal completion back onto the Home Assistant event loop and re-check the relevant token before mutating any state.

The worker/future callback itself returns only immutable conversion bytes or an exception. It never mutates Home Assistant state directly.

On config-entry unload:

1. Mark the entry stopped.
2. Increment `lifecycle_epoch`.
3. Cancel scheduler, retry timers, and awaiting tasks.
4. Cancel queued conversion futures and abandon the running future without awaiting it; keep the one domain-scoped executor in `hass.data` so an unload/reload does not accumulate executor threads.
5. Return from unload without waiting for a running conversion worker.
6. Reject every late completion through the lifecycle check.

The executor is created once per Home Assistant instance with `max_workers=1`, reused across the single supported entry's unload/reload, and shut down once during Home Assistant final stop with `wait=False` and `cancel_futures=True`. A running worker may therefore keep final interpreter exit waiting; prompt config-entry unload is the HACS V1 guarantee.

### Bounded image pipeline

Resolve a root-relative image path against the Larapaper base. When the image-base override is set, copy only protocol, hostname, and port; preserve the source path and query and ignore the override path and query.

Local/private image access is an explicit administrator trust decision, not an unrestricted SSRF exception. The only approved private origins are the normalized scheme, hostname, and effective port of the configured Larapaper base and optional image-base override. Root-relative images may use those origins; every other absolute or redirected destination must resolve only to public/global unicast addresses, and mixed public/private answers fail closed. Enforce that decision in the connection-resolution path so DNS rebinding cannot bypass a syntax-only preflight. Apply it to the initial URL and every redirect.

Handle image redirects manually with automatic redirects disabled. Follow only 301, 302, 303, 307, and 308; allow at most three redirects (four total requests), resolve relative `Location` values against the current URL, and reject missing/malformed locations, credentials, fragments, non-HTTP(S), HTTPS-to-HTTP downgrade, or a destination that fails the private/public rule. One 10-second timeout covers DNS, connection, TLS, the complete redirect chain, and final body consumption. Never send or forward Larapaper `ID`, `Access-Token`, integration credentials, `Authorization`, `Cookie`, or other source-origin headers to an image host.

For Content-Type:

- Missing or `application/octet-stream` may use magic bytes.
- Declared `image/png` or `image/bmp` must match magic bytes.
- Every other declared type fails.

Enforce maximum input size with early Content-Length checks and streamed byte counts. Before Pillow decode/load, parse dimensions where possible and enforce fixed `MAX_DECODED_PIXELS = 16777216` and `MAX_DECODED_DIMENSION = 8192` limits. Reject invalid dimensions, truncated data, Pillow decompression-bomb warnings/errors, and over-limit decoded images as `image_validation_failed`; do not attempt a memory-heavy decode. Enforce maximum converted output size before publication. A valid PNG passes through. BMP converts to PNG with Pillow. `filename` and `special_function` remain untrusted unused metadata. No image output reaches disk.

Use one domain-scoped `ThreadPoolExecutor(max_workers=1)` and one domain-scoped nonblocking admission slot stored together in `hass.data`, never Home Assistant's default executor. Both survive config-entry unload/reload so a new runtime cannot queue work behind an abandoned worker. At most one conversion is submitted or running, no conversion waits in the executor queue, and no second input payload is retained. Admission remains occupied until the underlying future actually completes, including after logical timeout, cancellation, cycle replacement, or unload. Completion releases only its own slot on the HA event loop; publishing or changing diagnostics, timestamps, metrics, availability, or timers still requires matching lifecycle and cycle tokens.

Cancellation semantics are intentionally abandonment-only for HACS V1. At a display deadline, increment `cycle_generation` first on the HA event loop, then cancel/abandon old async fetch and conversion tasks with no intervening await, and start the next display call. A running Pillow worker may finish because executor threads cannot be forcibly killed, but its result must be discarded and it must not delay display cadence or mutate state. If conversion admission is occupied, discard the newly fetched input without submitting or queueing it, record `image_conversion_failed`, and schedule only the normal captured-URL retry if its deadline is strictly before the next display deadline. A conversion timeout is a 10-second logical await/publication deadline, not a physical worker-kill guarantee. On config-entry unload, cancel queued work and abandon the running future without awaiting the domain-scoped executor; at Home Assistant final stop, shut down that executor with `wait=False` and `cancel_futures=True`. Config-entry unload returns promptly, but Home Assistant process exit may still wait for a running executor worker. A hard physical conversion kill requires process isolation and is not part of HACS V1.

Image or conversion failures retry only the captured resolved URL at +5, +10, +20, +40, then +60 seconds repeatedly, provided the retry belongs to the current lifecycle epoch and cycle generation and is strictly before the next display deadline. A retry due at or after the next deadline is skipped and its URL is abandoned.

### Cache, camera, diagnostics, and status

Store only an immutable last-good PNG, its monotonic receipt instant, and a UTC success timestamp in memory. Home Assistant restart is cold; no disk image cache exists.

`CameraEntity.async_camera_image()` returns the cached PNG bytes without network I/O when the cache is fresh. Freshness age uses the monotonic receipt instant; UTC exists only for diagnostics and wall-clock changes cannot alter freshness. At cold start or age equal to maximum stale, return `None`; never call `/api/display` from a camera read. Set content type to `image/png`.

Expose safe integration diagnostics:

- `status`: `ready`, `starting`, `stale`, or `error`.
- `ready`: true only when a fresh image is serveable.
- `stale`: true at age equal to or greater than maximum stale when a cache exists.
- `last_success_at`: UTC timestamp or null.
- `last_success_age_seconds`: nonnegative integer or null.
- `last_error`: one fixed allowlisted code or null.
- `next_display_at`: UTC timestamp or null.
- `next_retry_at`: UTC timestamp or null.

Status precedence is `ready` when a fresh image is serveable, otherwise `stale` when an expired cache exists, otherwise `error` when no image is serveable and a failure exists, otherwise `starting`. A fresh cache may therefore be `ready` while `last_error` is nonnull. The camera becomes unavailable when no fresh image is serveable.

The fixed error-code allowlist is:

```text
setup_auto_assign_disabled
setup_failed
display_failed
invalid_display_response
image_url_missing
image_fetch_failed
image_validation_failed
image_conversion_failed
internal_error
```

Diagnostics contain no raw exception, response body, URL, query, header, credential, Pillow error, or converter stderr. Native Home Assistant diagnostics are a local state projection and perform no network requests. All state mutation occurs on the Home Assistant event loop after epoch/generation checks.

### Security and network boundary

The HACS integration necessarily moves the network boundary into Home Assistant:

- Home Assistant reaches Larapaper and the returned image host.
- Home Assistant sends Larapaper credentials only to Larapaper API endpoints.
- Image hosts receive no Larapaper or integration credentials.
- Home Assistant's authenticated camera proxy serves the camera to dashboards.
- No public bridge port, bridge Basic auth, TLS reverse proxy, or Generic Camera credential configuration is required.

The integration stores the Larapaper API key in private HA-managed state. It must not log secrets or place them in entity attributes, diagnostics, URLs, exception text, or test output.

## V1 HACS release deliverable

A HACS V1 release is complete only when a clean Home Assistant instance can install the integration, complete the config flow, persist identity across restart, provision one Larapaper device, render a native camera entity, recover from display/image failures, and unload the config entry without stale work mutating state. Config-entry unload must return promptly with blocked conversion work; prompt Home Assistant process exit is not guaranteed while an abandoned executor worker is still running. No standalone container is required or expected for HACS V1.

Native dashboard verification uses the Home Assistant camera entity with Picture Entity or Picture Glance; no custom dashboard JavaScript is required. Repeated camera proxy/dashboard refreshes must not increase Larapaper `/api/display` calls or make any image-host/network requests.

The previous TypeScript bridge tests remain protocol-reference evidence. New HACS tests must run in a Home Assistant test environment and cover config flow, Store persistence, fake-clock scheduling, image validation/conversion, lifecycle cancellation, camera reads, diagnostics, and repeated-refresh behavior.

## Superseded external bridge plan

The following are deliberately not HACS V1 requirements and must not be reintroduced accidentally:

- Bun runtime
- ImageMagick probing or installation
- `Dockerfile`, `.dockerignore`, or `compose.yaml`
- `.task/larapaper-bridge-state.json`
- `/health` or `/image` HTTP routes
- Bridge Basic auth
- Listener startup deadlines
- Generic Camera Still Image URL configuration
- Public host port
- Disk image cache
- Display calls initiated by any Home Assistant read

If standalone non-HA support returns, create a separate bridge plan rather than mixing its HTTP/container contract into this HACS design.

## Ordered implementation backlog

### HA-HACS-01 — dedicated integration bootstrap, config flow, Store adapter, and pending identity

**Status / start condition:** Implementation-ready and first in the HA-HACS sequence. Start by creating or reserving the dedicated public repository `brettinternet/home-assistant-larapaper-bridge`; this work is not implemented in the current recipes repository. No Larapaper network or provisioning request is permitted in this item.
**Completed task:** Repository bootstrap — dedicated public repository exists at `https://github.com/brettinternet/home-assistant-larapaper-bridge`; package layout, minimum `0.1.0` manifest, and focused pytest harness committed as `fcd38e0` and `4e61193`.
**Completed task:** Config-flow validation and pending identity — implemented normalized URL/MAC/numeric fields, CSPRNG identity generation, duplicate/single-instance rejection, and pending Store persistence with focused HA tests in `a3339a2`.
**Review marker:** reviewed: `fcd38e0`, `4e61193`, `a3339a2` [review-fix: `3669b1a`, `a8d1f45`]; verified: `pytest -q tests/components/larapaper_bridge` — 29 passed; manifest/config-flow import passed.

**Goal / product intent:** Bootstrap one native Home Assistant `larapaper_bridge` integration and its test harness. Collect and persist validated configuration plus a stable synthetic device identity so a flow interrupted after persistence can resume with the same MAC. Leave all network, lifecycle, polling, image, and release behavior to later ordered items.

**Exact targets:** In the dedicated repository, create the ordinary Home Assistant integration layout under `custom_components/larapaper_bridge/`: `manifest.json`, `__init__.py`, `config_flow.py`, `const.py`, `strings.json`, `translations/en.json`, and `storage.py`. `custom_components/larapaper_bridge/storage.py` defines `LarapaperStore`, the sole Store-adapter path and symbol. Add `tests/components/larapaper_bridge/test_config_flow.py` and `test_storage.py` using the Python 3.14/pytest Home Assistant test harness. The minimum manifest uses the exact fields and `0.1.0` values settled below. Root `hacs.json`, brand, README, and release metadata belong to HA-HACS-05. Do not modify the recipes repository or superseded TypeScript bridge.

**Dependencies / order:** HA-HACS-01 has no implementation dependency and precedes HA-HACS-02. Its config-entry data and Store contract are the inputs consumed by HA-HACS-02. HA-HACS-03 extends the lifecycle state owned by HA-HACS-02; HA-HACS-04 owns image transport and typed outcomes; HA-HACS-05 projects/verifies the completed package and must not reimplement these runtime contracts.

**Scope and ownership:**

* Repository bootstrap: reserve/create the dedicated repository, establish the normal integration and pytest layout, and provide the minimum valid manifest with domain `larapaper_bridge`, name `Larapaper Bridge`, version `0.1.0`, documentation `https://github.com/brettinternet/home-assistant-larapaper-bridge`, issue tracker `https://github.com/brettinternet/home-assistant-larapaper-bridge/issues`, code owner `@brettinternet`, `config_flow: true`, `integration_type: device`, `iot_class: local_polling`, and `single_config_entry: true`. Keep exactly one integration, `larapaper_bridge`.
* Config flow: implement the user-facing fields and validation without performing network I/O. Trim all submitted strings. `base_url` is required, must be HTTP(S), have no credentials, query, or fragment, preserve any pathname prefix, and canonicalize to exactly one trailing slash. `image_base_url` is optional (`str | None`); blank becomes `None`; when present it must be an HTTP(S) origin with no credentials, non-root path, query, or fragment, and is stored as the normalized origin. `mac` is optional; blank becomes `None`; a supplied value must be exactly six colon-delimited hexadecimal octets and is stored uppercase canonical form. If omitted, identity generation uses a CSPRNG and produces an uppercase locally administered, unicast MAC.
* Numeric fields: trim input and use these exact defaults and constraints: `min_poll_seconds` defaults to `60`, is a finite positive float (decimal values accepted); `max_stale_seconds` defaults to `3600`, is a positive integer; `max_image_bytes` defaults to `10485760`, is a positive integer. Invalid, non-finite, zero, negative, fractional-integer, or otherwise unparsable values are rejected by the flow. Do not add fields or infer additional runtime policy.
* Identity and Store: use exactly `Store(hass, 1, 'larapaper_bridge', private=True, atomic_writes=True)`, with the fixed domain-wide key. Validate the HA storage envelope and payload versions independently. The payload is exactly either pending `{version: 1, mac}` or complete `{version: 1, mac, api_key, friendly_id}` (no extra payload keys); normal HA Store envelope metadata remains the adapter's responsibility. Load existing state before selecting a MAC. A configured MAC must match persisted state; an unconfigured MAC reuses a persisted MAC and only generates one when no state exists. Persist pending state before calling `async_create_entry`; only after that write succeeds may the flow create its config entry. The complete-state write/reuse contract is retained for the setup consumer, but no setup request is made here. Malformed or version-mismatched state is a permanent validation error, not a retry path.
* Config-entry identity: set the unique ID to exactly `larapaper_bridge` and reject a duplicate before any network access. Persist the validated config data needed by later items; do not persist secrets or invent API responses in this item.

**Non-goals:** Any Larapaper HTTP/WebSocket/network call; setup/provisioning client; retries; display polling; lifecycle coordinator, epoch, stopped state, cycle generation, deadlines, cache, or image retry policy (HA-HACS-02/03); image downloading, validation, transport, executor, admission, timers, camera/entity, diagnostics, routes, authentication, Docker, ImageMagick/Pillow packaging work, model/playlist assignment, multi-device support, disk image caching, root HACS metadata, brand artwork, or release automation.

**Resolved decisions / assumptions (with evidence and rationale):**

* The implementation home is the future public repository `brettinternet/home-assistant-larapaper-bridge`, while this backlog remains in the recipes repository. This is an explicit product/package decision in the shared HA-HACS contract; separating the installable HACS package prevents the backlog repo from becoming the integration artifact and gives HA-HACS-05 a stable release target.
* Minimum Home Assistant version is `2026.7.0`. This is the shared floor decision; target the current Store/config-flow APIs at that floor rather than adding compatibility branches for older releases. The skeleton manifest version is settled at `0.1.0`; HA-HACS-05 changes it to `1.0.0` only on the release commit.
* No Pillow manifest requirement is needed: HA `2026.7.0` provides Pillow `12.2.0`. This avoids a redundant dependency declaration and is the recorded packaging oracle decision; image implementation remains out of scope here.
* URL and state semantics are ported from the allowed protocol references only: `scripts/larapaper-bridge.ts` and `scripts/larapaper-bridge.test.ts`. The tests establish rejection of credentials/query/fragment and non-HTTP(S) base URLs, pathname-prefix preservation and trailing-slash normalization, image origin-only normalization, numeric defaults/constraints, uppercase MAC canonicalization, generated MAC bit requirements, exact pending/complete state shapes, and pending-before-setup ordering. HA Store's documented private/atomic options establish the exact adapter constructor; use no custom file format or parallel persistence path.
* The config flow must write pending identity before `async_create_entry`, even though this item performs no setup. This ordering is required by the interrupted-flow contract: a failure between the write and entry creation must leave a reusable pending MAC, and the next flow must load it without a setup request.

**Open questions resolved:** The optional image-base URL must already be an origin: credentials, a non-root path, query, and fragment are rejected rather than silently discarded. This makes the administrator-approved private-origin boundary explicit and avoids accepting misleading unused URL components. Blank optional strings normalize to `None`, matching the protocol tests and avoiding an ambiguous “configured but empty” state. The Store key is domain-wide and fixed (`larapaper_bridge`), not per-entry, because exactly one integration/identity is allowed and duplicate unique IDs are rejected. No additional network reachability validation is added because it would violate the no-network boundary and make config flow depend on remote availability.

**Acceptance:**

1. The dedicated repository contains the minimum valid `larapaper_bridge` package skeleton and Python 3.14/pytest HA harness targets, with exactly one integration; no root HACS manifest, brand, or release files are introduced by this item.
2. Focused config-flow tests prove trimming, required `base_url`, HTTP(S)-only validation, no credentials/query/fragment, prefix preservation, one trailing slash, image origin-only normalization, blank optional `None`, canonical uppercase MACs, generated locally administered/unicast MAC bits, and the exact numeric defaults/constraints above.
3. Focused Store tests prove construction with `Store(hass, 1, 'larapaper_bridge', private=True, atomic_writes=True)`, fixed key, independent envelope/payload version validation, exact pending and complete payload keys, malformed-state rejection, configured/state MAC mismatch rejection, and no extra payload fields.
4. A focused interrupted-flow test injects a stop/failure immediately after the pending Store save and before `async_create_entry`; a subsequent flow reuses the same MAC and performs no setup/network request. A duplicate unique ID is rejected before any network access. Complete state is reused and restart round-trips through the adapter; no image bytes are written.

**Pending verification:** Run only the dedicated repository's focused HA tests under `tests/components/larapaper_bridge/` for config flow, Store validation, pending ordering, duplicate identity, and restart round-trip. Confirm the minimum manifest parses against the HA `2026.7.0` floor and that test fixtures do not make live network calls. HACS/root metadata, brand, and release verification are explicitly pending HA-HACS-05.

**Last known evidence:** The existing protocol references show `canonicalizeMac` uppercasing six colon-delimited octets and `generateMac` setting `(byte[0] & 0xfc) | 0x02`; config tests cover the URL, blank/default, and numeric rules; state code/tests define version `1`, exact pending/complete shapes, MAC mismatch errors, and pending-before-provision ordering. Home Assistant core's `homeassistant/helpers/storage.py` and `homeassistant/util/file.py` document private atomic Store persistence. No evidence authorizes network behavior in this item.

**Biggest non-obvious risk:** A config-flow implementation can accidentally create the entry before the pending Store write has durably completed, causing MAC regeneration after an interruption. The test must assert call order and force the boundary failure, not merely inspect final state.

**Optional out-of-scope idea:** A future migration helper could report/repair invalid legacy Store envelopes, but HA-HACS-01 must treat malformed or version-mismatched state as permanent validation failure and must not add migration behavior speculatively.

**Next action:** Create/reserve `brettinternet/home-assistant-larapaper-bridge`, implement the package skeleton and exact config/Store contracts above, then add and run the focused tests before handing the config-entry and persistence interfaces to HA-HACS-02.

### HA-HACS-02 — provisioning client, lifecycle coordinator, and Larapaper display client

**Status / start condition:** Implementation-ready: yes. Start only after HA-HACS-01 has been accepted, including its validated config-entry data, private Store adapter, pending/complete state schema, and identity-selection contract. Begin by creating/reserving the dedicated future public repository `brettinternet/home-assistant-larapaper-bridge`; this work belongs there, not in this recipes repository. The current backlog remains in this recipes repository.
**Completed task:** One-shot Larapaper client — implemented shared-session `/api/setup` and `/api/display` operations with pathname-prefix URL joining, exact headers, redirect refusal, end-to-end 10-second timeout, typed safe failures, credential validation, display-rate/image normalization, and focused tests in dedicated-repository commit `871d04d`.
**Completed task:** Provisioning and lifecycle coordinator — implemented stable pending/complete Store transitions, setup retry backoff, concurrent provisioning sharing, lifecycle epochs, unload cancellation, retry-handle ownership, and focused tests in dedicated-repository commit `e3d3ca9`.
**Review marker:** reviewed: `871d04d`, `e3d3ca9` [review-fix: `8568e40`, `485f75a`, `d927015`]; verified: `pytest -q tests/components/larapaper_bridge` — 56 passed.

**Goal / product intent:** Provision or reuse exactly one synthetic Larapaper device for one Home Assistant config entry, persist its credentials safely, and expose one validated display result per scheduler request. The runtime must be lifecycle-safe: setup retries may continue only while the current config-entry lifecycle is active, and unload/reload must prevent stale work from committing state. A camera read or any other HA read must never initiate a Larapaper request.

**Exact targets:** In the dedicated repository, implement `custom_components/larapaper_bridge/client.py`, `provisioning.py`, and `runtime.py`. `custom_components/larapaper_bridge/runtime.py` defines the domain-scoped `RuntimeHolder` and per-entry `EntryRuntime`; these are the sole lifecycle coordinator symbols and own the monotonically increasing lifecycle epoch, stopped state, task registration/cancellation, setup retry, and complete-state persistence. Add `tests/components/larapaper_bridge/test_client.py` and `test_provisioning.py`. Use the shared session returned by `homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)`; do not create or close a private Larapaper session.

**Dependencies / order:** HA-HACS-01 is a hard prerequisite: consume its base URL (including any pathname prefix), canonical MAC, Store adapter, and pending/complete state objects rather than recreating validation or persistence. HA-HACS-03 consumes this coordinator's lifecycle fencing, complete credentials, and typed display outcomes to implement cycle deadlines/cache/image retries. HA-HACS-04 owns image transport and conversion. HA-HACS-05 projects and verifies the resulting integration and must not duplicate these runtime contracts. Do not add scheduler, image, camera, or release behavior here.

**Scope — lifecycle and provisioning:**

- Create one domain runtime holder in `hass.data[DOMAIN]` whose monotonically increasing `lifecycle_epoch` survives entry unload/reload; it owns the current per-entry coordinator, `stopped`, registered setup/display tasks, and retry handles. A reload creates a coordinator with the holder's current epoch rather than resetting the counter. On unload/invalidation, set `stopped = True`, increment the holder epoch, cancel registered tasks/handles, and remove registrations. A completion is allowed to mutate HA state or persist complete credentials only if it captured the current epoch and the coordinator is not stopped. `asyncio.CancelledError` must propagate; never turn cancellation into a classified network failure or schedule a retry for it.
- Provision exactly once at a time. If HA-HACS-01 loaded a valid complete state for the requested MAC, return it without a setup request. Otherwise ensure pending `{version: 1, mac}` is persisted through the HA Store before the first network attempt (including a retry after restart), then call setup. On success persist exactly complete `{version: 1, mac, api_key, friendly_id}` through the Store and return it. Preserve the pending MAC across every failed attempt and across restart; reject a Store snapshot whose MAC differs from the requested identity as a permanent persistence/state error.
- Retry setup only while the captured lifecycle epoch remains current. A retryable setup outcome is network/transport failure, timeout, malformed/non-JSON or otherwise protocol-invalid setup response, any unexpected HTTP status, and HTTP 404. Delay retries at `+5s`, `+10s`, `+20s`, `+40s`, `+60s`, then repeat `+60s` until success or lifecycle invalidation. HTTP 404 must use code `setup_auto_assign_disabled` and an actionable message telling the operator to enable `assign_new_devices`; it is still retryable because that account setting may be enabled without restarting HA. Before each retry, re-check epoch/stopped and reload Store state so an externally updated complete state is observed. Never retry malformed Store data, MAC mismatch, Store read/write/atomic-persistence errors, or cancellation; propagate those immediately. A stale retry must not write complete state after unload.

**Scope — one-shot client contract:** `client.py` performs no scheduling and no image work. Construct `/api/setup` and `/api/display` with URL joining that preserves a configured base pathname prefix (for example `https://host/bridge/` becomes `https://host/bridge/api/display`), without dropping or duplicating the prefix. Use the shared HA `aiohttp` session, `GET`, and `allow_redirects=False` for both calls. Each operation has one total 10-second budget covering connection, response headers, and complete body consumption; use an `aiohttp` timeout configuration that applies to the whole request/body, not only connection establishment. Do not log or include response bodies, API keys, or tokens in exception text.

- Setup sends exactly one `ID: <canonical MAC>` header and never sends `Access-Token`. Accept only a 2xx response whose JSON body is an object containing string `api_key` and `friendly_id` values that become nonempty after trimming. Return the trimmed values and ignore all extra fields. Classify 404 as `setup_auto_assign_disabled`; classify every other transport/timeout, redirect, non-2xx, JSON/decode, or credential-shape failure as `setup_failed`. Do not follow a redirect.
- Display sends exactly one `ID: <MAC>` and one `Access-Token: <api_key>` header. Accept only a 2xx JSON object with a finite numeric `refresh_rate > 0`. Return a typed success containing `image_url` normalized to `None` for missing, null, or empty string and `effective_interval_seconds = max(refresh_rate, configured minimum poll seconds)`. A non-string non-null `image_url` or invalid/missing/non-finite/non-positive rate is `invalid_display_response`. Transport/timeout, redirect, non-2xx, or JSON/decode failures are `display_failed`. A null/empty image URL is a valid display response and must not fetch an image or create an image retry. Display has no fast retry and exactly one HTTP attempt per caller invocation.
- Error classifications are stable typed outcomes/codes (`setup_auto_assign_disabled`, `setup_failed`, `display_failed`, `invalid_display_response`) with safe human-readable messages. Preserve underlying cancellation and Store exceptions as their original exception types; they are not Larapaper client outcomes.

**Scope ownership boundary:** HA-HACS-02 owns only the HTTP protocol adapter, setup/provisioning state transition, lifecycle epoch/stopped/task fencing, setup retry loop, and one-shot display response validation. HA-HACS-03 owns cycle generation, display deadlines, refresh/cache scheduling, stale boundaries, and image retry deadlines. HA-HACS-04 owns image URL transport, validation, executor admission, conversion, and typed image outcomes; it creates no timers. HA-HACS-05 owns camera/diagnostic projections and verification. Keep exactly one integration and one synthetic device.

**Non-goals:** No display scheduler or cycle-generation logic; no image URL fetching, content validation, Pillow conversion, cache publication, stale-image policy, camera entity, dashboard/entity behavior, diagnostics projection, HTTP server/routes, Basic Auth, Docker, ImageMagick, model/playlist assignment, multi-device support, or disk image bytes. Do not add a Pillow manifest requirement: HA `2026.7.0` already supplies Pillow `12.2.0`. Do not implement in this recipes repository or leave compatibility shims in the superseded TypeScript bridge.

**Resolved decisions / assumptions (with evidence and rationale):**

- Pin the Larapaper oracle to commit `bc114028354d2948fe868f938ed8d41de779b7ac`. `SetupController.php` and `ResolveDeviceByMacAddress.php` establish setup by MAC and the auto-assignment/404 behavior; `DisplayController.php` and `RunDeviceDisplayCycle.php` establish the display response and refresh cadence. Pinning avoids silently changing protocol semantics when upstream moves.
- Treat the existing TypeScript implementation/tests as reference fixtures, not implementation targets. `scripts/larapaper-bridge.ts` documents exact headers, `redirect: "error"`, 10,000 ms operation limits, URL-prefix joining, setup error codes, retry sequence, complete-state bypass, and display rate/image normalization; `scripts/larapaper-bridge.test.ts` exercises those observable contracts (especially lines covering setup/display requests, hanging bodies, retry delays, pending identity, and invalid rates). The Python tests must reproduce behavior with HA/aiohttp fakes rather than porting Bun APIs.
- Use HA's shared session because Home Assistant owns connector lifecycle, proxy/TLS behavior, and cleanup; a private session would leak sockets or violate integration conventions. `allow_redirects=False` is the aiohttp equivalent of the TS refusal and prevents credentials being forwarded to another origin.
- The 10-second timeout includes body consumption because a successful status with a hanging body is not a completed protocol operation; the TS hanging-body fixtures make this a consequential oracle requirement.
- Trim credential fields before accepting them because the contract is “trimmed nonempty”; return only the two fields so unknown server fields cannot become persisted state. This is stricter and safer than persisting opaque response objects.
- Retry 404 as well as network/protocol failures: the pinned setup behavior makes 404 actionable (`assign_new_devices`), and the reference retry tests deliberately allow the account setting to change while HA remains running. Store/persistence failures are local correctness faults, not transient Larapaper faults, so retrying could duplicate or conceal corruption.
- Epoch fencing is separate from task cancellation: cancellation is cooperative and an already-settled request can race unload. Checking epoch/stopped immediately before persistence prevents late credentials from an abandoned lifecycle entering the current entry.
- Default minimum HA version is `2026.7.0`; no Pillow manifest pin is required because that HA release provides Pillow `12.2.0`. Brand packaging is only `custom_components/larapaper_bridge/brand/icon.png`, and is outside this item.

**Open questions resolved:** No unresolved product or protocol questions remain. The upstream oracle does not require setup payload fields beyond `ID`; therefore send no query/body/Access-Token. The response contract is intentionally exact for required fields while ignoring extras, matching the TS fixture and avoiding forward-compatibility breakage. Missing/null/empty `image_url` is accepted because the pinned display cycle can represent no image and HA-HACS-04—not this client—owns image handling. A display error does not trigger a client-side retry because timing belongs exclusively to HA-HACS-03. Use the HA event loop and injected fake sleep/clock in tests rather than wall-clock sleeps; this is the deterministic default justified by the TS injected-sleep tests.

**Acceptance:**

1. `test_client.py` proves shared-session use, exact setup/display URLs with pathname prefixes, exactly one GET per invocation, exact header sets (setup has ID only; display has ID plus Access-Token), no redirect following, and a 10-second total timeout that aborts both a stalled request and a hanging response body while preserving `CancelledError`.
2. `test_client.py` proves setup accepts only 2xx JSON objects with trimmed nonempty `api_key`/`friendly_id`, returns only those fields, ignores extras, maps 404 to `setup_auto_assign_disabled` with `assign_new_devices` guidance, and maps all other setup failures to `setup_failed` without secret/body leakage.
3. `test_client.py` proves display accepts valid finite positive rates, computes `max(rate, minimum)`, normalizes missing/null/empty image URLs to `None`, rejects invalid rates and non-string image URLs as `invalid_display_response`, and maps transport/status/JSON failures as `display_failed` with no fast retry.
4. `test_provisioning.py` proves pending state is written before the first setup request, complete state is written only after successful setup, complete state bypasses setup, the MAC remains unchanged across failed retries/restart, and malformed Store/MAC mismatch/read-write errors do not retry.
5. `test_provisioning.py` proves retry delays are exactly 5/10/20/40/60/60… seconds, retries stop after unload or epoch change, cancellation propagates, registered tasks/handles are cancelled and removed, and stale completions cannot persist complete state. It also proves only one provisioning operation is active for the entry.
6. Tests use fake `aiohttp` responses/session, fake HA Store, fake clock/sleep, and deterministic task coordination; no live Larapaper credentials or network access. Scheduler timing, image transport/conversion, camera, cache, and release acceptance remain assertions of HA-HACS-03/04/05.

**Pending verification:** Run the two focused Home Assistant async test modules in the future bridge repository, then run the repository's targeted type/import checks and HA config validation once those files exist. Verify with an instrumented aiohttp fake that the timeout surrounds body reads and that `allow_redirects=False` is passed. Perform a reload/unload stress scenario to demonstrate no stale completion persists credentials. Do not run tests or gates in this backlog-writing task.

**Last known evidence:** Pinned Larapaper sources: [`SetupController.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Http/Controllers/Api/Firmware/SetupController.php), [`ResolveDeviceByMacAddress.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Actions/Api/ResolveDeviceByMacAddress.php), [`DisplayController.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Http/Controllers/Api/Firmware/DisplayController.php), and [`RunDeviceDisplayCycle.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac/app/Actions/Api/RunDeviceDisplayCycle.php). Reference protocol and TS tests: [`scripts/larapaper-bridge.ts`](../scripts/larapaper-bridge.ts), [`scripts/larapaper-bridge.test.ts`](../scripts/larapaper-bridge.test.ts), including setup/display header and redirect assertions, prefix joining, hanging-body timeout tests, null/empty image handling, invalid-rate classification, retry delays, pending identity preservation, complete-state reuse, and non-client-error no-retry behavior. Repository design evidence and shared decisions are recorded in `docs/home-assistant-hacs.md`.

**Additional resolved implementation detail:** Obtain the Larapaper session through `homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)`, the public Home Assistant helper at the `2026.7.0` floor. This import and ownership are fixed; do not substitute a private session.

**Biggest non-obvious risk:** A request can finish at the same time as unload, after task cancellation but before persistence. Epoch/stopped checks must guard every state mutation, and tests must force that race; otherwise an old setup can overwrite the newly loaded entry's credentials while appearing healthy.

**Optional out-of-scope idea:** A future diagnostics view could expose the last typed setup/display classification and retry attempt without exposing credential values; defer to HA-HACS-05.

**Next action:** In the reserved `brettinternet/home-assistant-larapaper-bridge` repository, implement `client.py` first, then provisioning/coordinator lifecycle fencing, then the two focused test modules; accept HA-HACS-02 before beginning HA-HACS-03 or HA-HACS-04.

### HA-HACS-03 — independent display scheduler, cache, and lifecycle fencing

**Status / start condition:** Implementation-ready: yes. Start only after HA-HACS-01 has created the integration entry/runtime state and HA-HACS-02 has accepted complete provisioning and exposed its typed display-result/failure contract. This item is independently executable with fake clock, fake coordinator, and fake image operation; it does not require a real HTTP client, Pillow, or executor implementation.
**Completed task:** Frozen image seam and first settlement-anchored display cycle — added immutable `ImageOutcome`, `CacheRecord`, `DiagnosticsState`, `OperationToken`, the `ImageOperation` protocol, runtime cycle fencing, and immediate one-call scheduling in dedicated-repository commit `79c64ac`; focused scheduler tests passed (3), integration tests passed (59).
**Completed task:** Invalid-rate and display-failure scheduling — records typed display errors, anchors the next deadline at settlement using the prior effective interval or configured minimum, and keeps the scheduler running for the next cycle in dedicated-repository commit `36a24b8`; `pytest -q tests/components/larapaper_bridge` passed (62).
**Completed task:** Cache publication/freshness and diagnostics projections — implemented immutable last-good PNG cache, monotonic freshness boundary, UTC projections, status precedence, safe image URL errors, and fixed-error allowlist redaction in dedicated-repository commits `015a11a`, `fb79dca`, and `2f6a27e`; focused scheduler tests passed (12), integration tests passed (68).
**Completed task:** Image retries, abandonment, and unload-race fencing — implemented captured-URL retries with strict next-display cutoff, nonblocking scheduler cadence, synchronous image abandonment before new cycles and unload, lifecycle/cycle publication fencing, retry diagnostics, and blocked-retry cancellation coverage in dedicated-repository commits `9b6d7f5`, `53a2788`; `pytest -q tests/components/larapaper_bridge/test_scheduler.py tests/components/larapaper_bridge/test_provisioning.py tests/components/larapaper_bridge/test_client.py tests/components/larapaper_bridge/test_config_flow.py tests/components/larapaper_bridge/test_storage.py` — 69 passed.
**Review marker:** reviewed: `79c64ac`, `36a24b8`, `015a11a`, `fb79dca`, `2f6a27e`, `9b6d7f5`, `53a2788`; verified: `pytest -q tests/components/larapaper_bridge` — 71 passed.

**Goal / product intent:** Poll exactly one provisioned Larapaper device at its effective cadence, retain the last safe PNG in memory, and ensure abandoned asynchronous work can never mutate the current Home Assistant entry. Home Assistant camera reads remain cache-only and never advance Larapaper.

**Exact targets:** Add `custom_components/larapaper_bridge/scheduler.py` and its immutable cache/diagnostic state types. Extend HA-HACS-02's `EntryRuntime` in `runtime.py`; do not create a second coordinator. Define the frozen typed image seam in `scheduler.py`; HA-HACS-04 implements it without redesign. Add `tests/components/larapaper_bridge/test_scheduler.py` with fake monotonic clock/timers, fake display client, and fake image operation that can block or complete after abandonment. No production `image.py`, HTTP, Pillow, session, executor, or admission implementation belongs here.

**Dependencies and order:** Consume HA-HACS-02's `EntryRuntime`, lifecycle epoch, credentials, effective-interval selection, display classifications, and setup callback. In `scheduler.py`, define immutable `ImageOutcome(png_bytes: bytes | None, resolved_url: str | None, error_code: ImageErrorCode | None)` and an `ImageOperation` Protocol with `async_process(url, token) -> ImageOutcome` plus synchronous `abandon(token) -> None`. `ImageErrorCode` has exactly `fetch`, `validation`, and `conversion`. Construction enforces exactly one of `png_bytes` and `error_code`; `resolved_url` is required for retryable errors and optional on success; no raw response or exception is carried. Verify against a fake implementation. HA-HACS-03 lands before HA-HACS-04, which implements this seam; HA-HACS-05 only projects and verifies the integrated state.

**Scope and ownership:**

* Maintain one scheduler per config entry, started only after complete provisioning. Start the first display attempt immediately. Use an injected monotonic clock (`clock.monotonic() -> float`) for all deadlines; capture the monotonic settlement instant when a display attempt is classified as accepted, invalid, or failed. Set `next_display_deadline = settlement + selected_effective_interval`; expose UTC conversion only as a diagnostic projection, never as a scheduling or freshness input.
* Make exactly one `/api/display` operation per cycle through HA-HACS-02. An accepted response selects its validated effective interval. Invalid/missing `refresh_rate` rejects the complete result, preserves the cache, records `invalid_display_response`, and uses the prior valid effective interval or configured minimum. A display failure has no fast retry because its side effect is ambiguous; settle it and schedule with the prior valid interval or configured minimum before the first valid response. Null, empty, or missing `image_url` records `image_url_missing`, preserves the cache, and schedules normally without image work. A non-string, non-null URL rejects the result as `invalid_display_response`.
* Own an immutable cache record containing PNG bytes (or no image), monotonic receipt instant, UTC receipt projection, source/cycle identity as needed for diagnostics, and no mutable byte buffer. Replace the whole record atomically on the HA event loop; never persist image bytes. The exact stale boundary is `age >= maximum_stale_seconds`; use monotonic receipt age, so wall-clock changes cannot affect camera freshness. Cache publication is allowed only for the current lifecycle epoch and cycle generation.
* Maintain diagnostics state (`status`, `ready`, `stale`, success timestamp/age, allowlisted `last_error`, next display/retry UTC projections) without secrets, URLs, exceptions, response bodies, or converter output. Apply the settled precedence: `ready` if a fresh cache exists; otherwise `stale` if an expired cache exists; otherwise `error` if no serveable image and a failure exists; otherwise `starting`. A fresh cache may remain `ready` while `last_error` is non-null.
* Keep separate tokens. `lifecycle_epoch` identifies the loaded config-entry lifetime and increments only on unload/invalidation. `cycle_generation` identifies one display cycle and increments exactly once at each display deadline. Every task/timer/image operation captures both applicable values and checks them before any state mutation. Setup completion checks lifecycle epoch only; it must still be accepted after cycle-generation changes, but must be rejected before Store/runtime mutation after lifecycle invalidation.
* At each display deadline, on the HA event loop, increment `cycle_generation` first; with no intervening await cancel cancellable async fetch/retry tasks and abandon prior image work; then begin the new display call. A late image result is discarded if either token is stale. Worker completion is marshalled back to the HA event loop before token checks or publication; executor threads never touch HA state.
* Delegate image work through the item-owned typed protocol. The first outcome supplies the captured resolved URL; every retry passes that exact URL back to `async_process`. Image/conversion failure schedules retries at exactly +5, +10, +20, +40, then +60 seconds, repeating +60 seconds thereafter, measured from each failure settlement. Schedule a retry only when its absolute monotonic due time is strictly before the current cycle's next display deadline. A retry due at or after that deadline is skipped and the URL abandoned. Retry timers capture epoch and cycle tokens and are cancelled/abandoned when the next cycle starts or the entry unloads. Image recovery never recalls `/api/display`.
* On unload, mark `EntryRuntime` stopped before cancellation, increment `lifecycle_epoch`, cancel scheduler/retry/awaiting tasks, call synchronous `abandon(token)` for running image work without awaiting it, and return promptly. HA-HACS-03 owns abandonment only; HA-HACS-04 registers and owns final-stop closure of the image session and shutdown of the executor; HA-HACS-05 verifies that behavior. No late completion may mutate cache, timestamps, diagnostics, metrics, availability, or retry state after unload.

**Non-goals:** Implementing HTTP routes, URL resolution, redirects, TLS, response/body limits, Pillow/BMP conversion, executor admission, dashboard entities, camera reads, public health JSON, Basic auth, Docker/ImageMagick, disk image storage, multiple devices, display recall, or a second lifecycle coordinator. Do not add timers inside the typed image operation.

**Resolved decisions and assumptions (oracle rationale):**

* The dedicated future public repository is `brettinternet/home-assistant-larapaper-bridge`; this backlog and implementation planning remain in the current recipes repository. HA-HACS-01 begins by creating/reserving that repository. This avoids publishing a HACS integration from a repository whose primary product is unrelated recipes while preserving this ordered contract.
* Minimum Home Assistant is `2026.7.0`. Use ordinary HACS integration layout and root `hacs.json`; include exactly one integration and put the only brand asset at `custom_components/larapaper_bridge/brand/icon.png`. No Pillow manifest requirement is permitted because HA 2026.7.0 supplies Pillow 12.2.0. These packaging decisions are shared release oracles, not scheduler behavior.
* Settlement anchoring is mandatory: scheduling from request start or response receipt would shorten cadence during slow calls and can overlap side-effecting display calls. The pinned `DisplayController.php`/`RunDeviceDisplayCycle.php` show accepted display requests have side effects, so one call per cycle and no ambiguous fast retry are safer than speculative retry.
* Monotonic time owns deadlines/freshness; UTC is only a projection because wall-clock corrections must not make an image fresh or stale. The exact stale equality (`>=`) and strict retry-before-display inequality prevent boundary races.
* Two tokens are required because an entry can reload (lifecycle invalidation) while a newer display cycle remains valid, and a setup completion can arrive after a cycle-generation increment. Combining them would either accept stale reload work or reject valid setup completion.
* Abandonment, rather than forced thread cancellation, is required because Python executor threads cannot be forcibly killed. HA-HACS-04 owns typed operation/executor details; this item owns immediate cadence and fencing around abandonment.

**Open questions resolved:** The retry sequence is fixed to +5/+10/+20/+40/+60 seconds, then repeated +60 seconds, and is relative to each captured image-operation failure settlement. The scheduler owns these timers; HA-HACS-04 returns outcomes only. The first display is immediate, and every later deadline is settlement plus the selected interval. Null image URLs create no retry. A retry at equality with the next display deadline is skipped. These defaults follow the settled operations contract and avoid an extra display request or unbounded retry storm.

**Acceptance:** `test_scheduler.py` proves one display call per cycle; immediate first call; settlement-anchored monotonic deadlines during slow responses; accepted effective-rate behavior; invalid-rate and display-failure scheduling; null/empty URL behavior; immutable cache replacement; exact stale boundary; diagnostics precedence/projections; captured-URL retry sequence and strict deadline cutoff; no display recall. It proves cycle-generation increment-before-abandonment ordering, separate lifecycle/cycle token behavior, current-generation-only publication, event-loop-marshalled worker completion, completion racing cancellation, setup completion accepted across cycle changes but rejected after lifecycle invalidation, prompt unload with blocked conversion, queued-future cancellation and running-future abandonment, no worker-thread HA mutation, timer cleanup, and no late mutation after unload. The fake image operation must assert typed outcomes and abandonment calls, not implement real image transport.

**Pending verification:** Run only the focused Home Assistant scheduler test module with injected fake clock/event loop and blocked-worker scenarios after implementation. HA-HACS-04 separately verifies transport/conversion/admission; HA-HACS-05 verifies camera/diagnostics projection and final-stop executor shutdown. No live network, Pillow, formatter, linter, or project-wide gate is part of this item.

**Last known evidence:** The retained architecture states that only the independent scheduler calls `/api/display`, camera reads are cache-only, display failures have no fast retry, image retries use the captured absolute deadline, stale equality is `age >= maximum_stale_seconds`, and unload abandons executor work without awaiting. Pinned Larapaper display sources establish side effects; Python asyncio/executor cancellation semantics establish the abandonment and token-fencing requirement.

**Additional resolved implementation detail:** The image seam is frozen in `scheduler.py`, not left for HA-HACS-04 to design. `ImageOutcome` has exactly `png_bytes`, `resolved_url`, and `error_code`; `ImageErrorCode` has exactly `fetch`, `validation`, and `conversion`; constructor invariants enforce one success/error payload and a resolved URL on retryable errors. `ImageOperation` exposes only `async_process(url, token)` and synchronous `abandon(token)`. HA-HACS-04 implements the protocol and owns final-stop session/executor shutdown without changing scheduler timing or retries; HA-HACS-05 verifies it.

**Biggest non-obvious risk:** A late worker completion can appear successful after a newer cycle or unload and silently overwrite a good cache unless both tokens are checked on the HA event loop immediately before every publication and diagnostic mutation; cancellation alone is insufficient.

**Optional out-of-scope idea:** A future version may expose structured per-cycle timing metrics, but such metrics must remain event-loop projections and cannot alter scheduling or become a new public contract.

**Next action:** Implement `scheduler.py` and immutable state types against the HA-HACS-02 coordinator and fake typed image operation, then add and run `tests/components/larapaper_bridge/test_scheduler.py` through the acceptance matrix before starting HA-HACS-04.

### HA-HACS-04 — bounded image transport, validation, and conversion

**Status / start condition:** Implementation-ready: yes. Start only after HA-HACS-01, HA-HACS-02, and HA-HACS-03 are accepted. Implement and test this item only in `brettinternet/home-assistant-larapaper-bridge`; the backlog remains planning-only in the recipes repository.
**Completed task:** URL resolution — implemented pathname-prefix-preserving HTTP(S) image URL normalization, origin-only image-base override, source path/query preservation, unsafe URL rejection including protocol-relative and empty query/fragment forms, and focused tests in dedicated-repository commits `3d9ae29`, `a23cb15`, `26713a6`; `python -m pytest -q tests/components/larapaper_bridge/test_image.py` — 28 passed; full integration suite — 99 passed.
**Completed task:** Connection-time SSRF policy and bounded image response transport — implemented policy-aware connection resolution with exact configured private-origin exceptions, global-unicast-only unconfigured destinations, mixed-answer and per-resolution rebinding rejection, manual redirects with one end-to-end timeout, streamed inclusive byte limits, Content-Length preflight, media-type/magic validation, and safe fetch outcomes in dedicated-repository commits `4bd10ef`, `99f3b2f`, and `9b53846`; `python -m pytest -q tests/components/larapaper_bridge/test_image.py` — 37 passed; full integration suite — 66 passed.
**Completed task:** Decode boundary and bounded BMP conversion — added PNG/BMP structural dimension preflight, fixed decoded dimension/pixel limits, Pillow-only BMP conversion, decompression-bomb/truncation handling, converted-output limits, and filesystem-free focused coverage in dedicated-repository commit `c4bdb1f`.
**Completed task:** Domain-scoped admission and late-completion fencing — added one-worker conversion admission, final-stop resource cleanup, timeout/cancellation abandonment, lifecycle-safe future release, and image-policy reuse fencing in dedicated-repository commits `d871b90`, `3a9d09b`, and `c625cf9`.
**Review marker:** reviewed: `3d9ae29`, `a23cb15`, `26713a6`, `4bd10ef`, `99f3b2f`, `9b53846`, `c4bdb1f`, `d871b90`, `3a9d09b`, `c625cf9` [review-fix: `db61d2e`]; verified: `python -m pytest -q tests/components/larapaper_bridge/test_image.py` — 92 passed; `python -m pytest -q tests/components/larapaper_bridge` — 163 passed.

**Goal / product intent:** Turn the URL returned by one accepted Larapaper display result into one immutable, bounded PNG (or a typed safe failure), without leaking Larapaper credentials, allowing SSRF, blocking display cadence, or permitting abandoned work to publish state. This item supplies the image pipeline; HA-HACS-03 owns lifecycle/cycle fencing, deadlines, retries, cache publication, and all timers.

**Exact targets:** In `brettinternet/home-assistant-larapaper-bridge`, implement `custom_components/larapaper_bridge/image.py` and `tests/components/larapaper_bridge/test_image.py`. HA-HACS-04 exclusively owns the domain-scoped image `aiohttp.ClientSession`, policy-aware `TCPConnector`/resolver, one-worker executor, admission slot, Home Assistant final-stop callback registration, and final-stop session `close()` plus executor `shutdown(wait=False, cancel_futures=True)`. Store and reuse these resources in `hass.data[DOMAIN]` across entry unload/reload. Disable DNS caching; the resolver returns only classified addresses actually used by the connector while preserving the original hostname for TLS. Do not use HA's ordinary shared session for image traffic. HA-HACS-05 verifies but never owns these resources.

**Dependencies / order:** Consume HA-HACS-01's validated Larapaper base URL, optional image-base URL, maximum image-byte setting, and credentials only as an already-authenticated display result; consume HA-HACS-02's display-result contract; consume HA-HACS-03's captured `OperationToken(lifecycle_epoch, cycle_generation)`, monotonic deadlines, cancellation/abandonment protocol, and retry classification. HA-HACS-04 must not create or own timers, retries, cycle generations, lifecycle epochs, cache entries, timestamps, availability, diagnostics, or metrics. HA-HACS-05 may project only the typed outcomes and verified runtime contracts produced here.

**Scope and ownership:**

1. **URL resolution and request construction.** Resolve root-relative `image_url` against the configured Larapaper origin, preserving the configured pathname prefix, source pathname, and query according to HA-HACS-01's URL contract. If `image base URL` is configured, mechanically replace only scheme, hostname, and port with that origin; ignore override path/query and preserve source path/query. Accept only HTTP and HTTPS. Reject credentials, fragments, malformed URLs, unsupported schemes, and empty/invalid hosts. A source URL's `filename` and `special_function` are untrusted metadata and are never used for routing, headers, filesystem access, or conversion policy.

2. **Explicit SSRF policy (enforced in connection resolution, not syntax-only).** Normalize each URL's scheme/hostname/port and resolve its host immediately before every connection, including every redirect destination. For configured Larapaper and configured image-base origins, a private destination is allowed only when its normalized scheme, hostname, and effective port exactly equal that configured origin; path/query do not broaden authority. An unconfigured absolute source or redirect origin must resolve solely to global-unicast addresses. If DNS returns any mixed set (global plus private, loopback, link-local, multicast, unspecified, reserved, or otherwise non-global address), fail the request; do not select the global answer. Reject DNS rebinding by resolving and checking the actual connection target each hop (and do not accept a hostname merely because its textual address looks public). If the transport/HTTP adapter cannot enforce this destination check in its connection-resolution path, fail acceptance rather than shipping syntax-only filtering. Record this as a deliberate accepted private-origin authority risk: a user who configures a Larapaper/image-base origin is authorizing that exact private origin, so compromise or misconfiguration of that origin remains within the trust boundary; no arbitrary private destination is authorized.

3. **Manual redirect transport.** Disable automatic redirects and implement only HTTP 301, 302, 303, 307, and 308. Permit at most three hops, re-parse and apply the complete URL/SSRF policy at every hop, require a `Location` header, and resolve relative `Location` values against the current URL. Reject credentials, fragments, non-HTTP(S), malformed locations, and any HTTPS-to-HTTP downgrade. Preserve the single operation budget of 10 seconds across DNS, connect, TLS, headers, all response-body reads, and all redirect hops; never reset it per hop. Send no `ID`, `Access-Token`, API key, cookies, authorization, integration headers, or other Larapaper headers to image hosts. Follow no unrecognized redirect status; treat every non-redirect non-2xx response and every missing/invalid redirect location as terminal failure.

4. **Exact byte/type contract.** The configured maximum image bytes is an inclusive upper bound and applies to the encoded body. Reject a declared `Content-Length` greater than the limit before reading; stream in chunks and reject as soon as accumulated bytes exceed it, including chunked/no-length responses. Do not retain a second copy while enforcing the limit. Accept only a 2xx final response. A missing `Content-Type` or exact `application/octet-stream` may be identified by magic bytes. Exact `image/png` and `image/bmp` declarations must match their magic bytes. Every other declared media type fails, including HTML-like types. Compare the media type case-insensitively after stripping valid parameters, so parameterized `image/png`, `image/bmp`, and `application/octet-stream` follow the same magic rules as their bare forms; malformed parameters fail validation. PNG magic is the eight-byte signature `89 50 4E 47 0D 0A 1A 0A`; BMP magic is `42 4D`. Reject HTML, unknown, truncated, and content/type-conflicting bytes. Map all transport, status, URL, redirect, TLS, SSRF, body-limit, media-type, magic, truncation, and decoded-bound violations to typed `image_fetch_failed` or `image_validation_failed` outcomes (transport/HTTP/SSRF/final-response errors are fetch failures; body/type/content errors are validation failures); never expose raw exception text.

5. **Decode and Pillow boundary.** Parse and validate dimensions from every PNG and BMP header before any Pillow decode; preflight is mandatory. Reject non-positive, malformed, truncated, or overflow-prone dimensions; enforce `MAX_DECODED_DIMENSION = 8192` per axis and `MAX_DECODED_PIXELS = 16_777_216` before memory-heavy decode. In the worker, use Pillow only for BMP conversion, treat decompression-bomb warnings/errors as hard failures, reject truncation, verify dimensions/pixels again, convert BMP to PNG, and enforce converted-output size before return. Valid PNG returns unchanged immutable bytes after header/bounds validation. No ImageMagick, subprocess, shell, temporary file, cache file, or filesystem output.

6. **Executor and admission.** Create exactly one domain-scoped `ThreadPoolExecutor(max_workers=1)` and one domain-scoped nonblocking admission slot per Home Assistant instance; retain both across config-entry unload/reload. Register final-stop cleanup exactly once; close the image session and call executor `shutdown(wait=False, cancel_futures=True)` only at final stop. When occupied, discard newly fetched bytes immediately, retain no queued future or second payload, and return only `ImageErrorCode.conversion`, which maps to `image_conversion_failed`; busy is not a separate classification. Never await admission. Timeout, cancellation, unload, and abandonment do not release it; only the underlying future's done callback on the HA loop does. HA-HACS-04 creates no scheduler timer and publishes no cache/diagnostic state.

**Non-goals:** No display scheduler, retry timer, cache publication, CameraEntity read, lifecycle/cycle token implementation, diagnostics or availability mutation, Larapaper request, credential persistence, public image route, arbitrary image formats, HTML fallback, image-host authentication, redirects for Larapaper API calls, ImageMagick, subprocesses, filesystem output, or additional repository/integration. Do not add a Pillow manifest requirement: supported minimum HA `2026.7.0` already supplies Pillow `12.2.0`.

**Resolved decisions, assumptions, and oracle rationale:**

- Use minimum HA `2026.7.0`, ordinary standard integration layout, root `hacs.json`, exactly one integration, and brand only at `custom_components/larapaper_bridge/brand/icon.png` as the shared contract requires. The future public repository is `brettinternet/home-assistant-larapaper-bridge`, not this recipes repository.
- OracleRuntime's security recommendation is authoritative for this item: SSRF must be decided at connection resolution for every hop; global-unicast-only is required for unconfigured origins; mixed DNS answers fail closed; an exact configured-origin exception is the only private-address allowance. This is stronger than URL syntax filtering and is accepted despite the explicitly recorded private-origin authority risk.
- OracleRuntime's concurrency recommendation is authoritative: timeout/cancel means abandonment, not release; release admission only from the underlying future's done callback on the HA loop. A single shared one-worker executor prevents blocked conversion workers multiplying on reload.
- Manual redirects are selected over library auto-follow so destination policy, downgrade prevention, header stripping, and one end-to-end budget are observable and testable. Three hops and the five redirect status codes preserve the product contract already recorded in the pinned backlog.
- Pinned source evidence: Larapaper commit `bc114028354d2948fe868f938ed8d41de779b7ac`, [`DeviceImageResolver.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac), and [`config/filesystems.php`](https://github.com/usetrmnl/larapaper/blob/bc114028354d2948fe868f938ed8d41de779b7ac) establish local/external/S3/CDN image forms. Pinned HA commit `798888125a13838bce8a15b7b5f81fd9738334d5`, [`camera.py`](https://github.com/home-assistant/core/blob/798888125a13838bce8a15b7b5f81fd9738334d5/homeassistant/components/generic/camera.py), and current HA requirements establish the in-process camera/Pillow boundary. These pinned sources justify preserving path/query forms, using HTTP(S) image retrieval, and relying on bundled Pillow rather than adding a manifest dependency.

**Open questions resolved:** The dedicated repository is the sole implementation/test home. PNG and BMP dimensions are always header-preflighted before Pillow. Parameterized media types are compared case-insensitively after valid parameter stripping; malformed parameters fail validation. Busy admission returns only conversion failure. The image session, connector/resolver, executor, admission, final-stop registration, and close/shutdown belong exclusively to HA-HACS-04; HA-HACS-05 only verifies. Admission is domain-scoped, survives unload/reload, and releases only on underlying future completion.

**Acceptance and `test_image.py` security/concurrency matrix:** Add deterministic tests under `tests/components/larapaper_bridge/test_image.py` with fake DNS/connection resolution and fake HTTP responses. The matrix must include:

- URL resolution: root-relative paths, configured pathname prefixes, image-base origin-only override, source path/query preservation, credentials/fragments/non-HTTP(S)/malformed host rejection.
- SSRF: exact configured private Larapaper origin allowed; exact configured private image-base origin allowed; unconfigured private/loopback/link-local/multicast/unspecified/reserved rejected; unconfigured global-only accepted; mixed global+private DNS rejected; per-hop rebinding checked; textual public hostname resolving privately rejected; connection-resolution enforcement exercised (or a test proving unsupported enforcement causes acceptance failure).
- Redirects: each of 301/302/303/307/308, relative locations, three-hop success, fourth-hop rejection, explicit 300 and 304 responses with `Location` treated as terminal failures, other non-redirect non-2xx terminal failures, missing/invalid location, destination revalidation, HTTPS downgrade rejection, one shared 10-second budget across hops/body, and no forwarded credentials/headers.
- Type and bounds: 2xx requirement; missing/octet-stream magic fallback; exact PNG/BMP matching; conflicting declarations, parameters/unsupported types, HTML/unknown bytes, malformed/truncated signatures; early `Content-Length` rejection; streamed over-limit rejection; exact-at-limit success.
- Decode/conversion: malformed/non-positive/overflow dimensions; dimension 8192 boundary and above; pixel limit boundary and above; Pillow decompression-bomb warning/error; truncated BMP/PNG; valid PNG pass-through; BMP-to-PNG output and output-limit rejection; immutable returned bytes; no filesystem calls.
- Concurrency/lifecycle: occupied admission immediately discards a second payload without queueing/retaining it; one shared executor across unload/reload; timeout and cancellation do not release admission; unload returns while a worker ignoring cancellation continues; admission releases only when the underlying future completes; late completion yields no publication or mutation after epoch/cycle invalidation; repeated display deadlines preserve cadence; no timers/cache/diagnostic mutations originate in `image.py`.

**Pending verification:** Run the focused `tests/components/larapaper_bridge/test_image.py` suite in the eventual integration repository with Home Assistant's supported Python/Pillow environment; run HACS/package validation and a clean install only under HA-HACS-05. Verify the transport adapter truly applies SSRF checks to the socket connection target, not only parsed URL text, before release. No live network, credentials, or disk image output belongs in the tests.

**Last known evidence:** Existing backlog sections 190–208 define the byte, magic, dimension, Pillow, timeout, lifecycle, retry, and executor contracts. Pinned Larapaper and Home Assistant sources above are the current protocol/package evidence. The shared decision records HA `2026.7.0` with Pillow `12.2.0`, so no Pillow manifest requirement is needed.

**Additional resolved implementation detail:** Image traffic uses the domain-scoped policy-aware `aiohttp.ClientSession` and connector described above, not Home Assistant's ordinary shared connector. The connector consumes the resolver's validated address records directly, disables DNS caching, preserves the URL hostname for TLS/SNI, and is covered by connection-target tests; inability to enforce this is a failed acceptance criterion, not an implementation choice.

**Biggest non-obvious risk:** A timed-out Pillow conversion still owns the sole executor and admission slot until its non-cooperative worker exits; treating cancellation as completion silently permits queued work or overlapping conversions and can cause late state mutation. Tests must model a worker that ignores cancellation and spans multiple display deadlines.

**Optional out-of-scope idea:** A future platform transport with first-class DNS pinning/socket binding could replace the custom resolver, but only if it preserves the exact-origin exception, mixed-answer fail-closed rule, per-hop validation, and one-budget redirect contract.

**Next action:** After HA-HACS-03 acceptance, implement `image.py` and the complete focused `test_image.py` matrix in the reserved `brettinternet/home-assistant-larapaper-bridge` repository, then hand typed outcomes and verification evidence to HA-HACS-05; do not edit the current backlog file as part of this item.

### HA-HACS-05 — native camera, diagnostics, HACS packaging, and v1.0.2 forward-only release gate

**Status / start condition:** Implementation-ready. Start only after HA-HACS-01 through HA-HACS-04 pass their focused tests and freeze their contracts. HA-HACS-05 delivers and verifies release artifacts only in `brettinternet/home-assistant-larapaper-bridge`; this backlog remains planning-only in the recipes repository.
**Completed task:** Native cache-only camera and redacted diagnostics projections — implemented `camera.py` and `diagnostics.py` with immutable PNG reads, monotonic freshness/unavailability boundaries, fixed diagnostics whitelist, UTC serialization, status/error redaction, and focused tests in dedicated-repository commit `1c2cb62`; `python -m pytest -q tests/components/larapaper_bridge/test_camera.py tests/components/larapaper_bridge/test_diagnostics.py` — 8 passed; full integration suite — 171 passed.
**Completed task:** Release packaging metadata and brand provenance — added root `hacs.json`, manifest `1.0.0`, original 256×256 brand asset, exact SHA-256 provenance record, and focused bootstrap assertions in accumulated dedicated-repository commit `943cb71` (on `1c2cb62`); `python -m pytest -q tests/components/larapaper_bridge/test_bootstrap.py tests/components/larapaper_bridge/test_camera.py tests/components/larapaper_bridge/test_diagnostics.py` — 11 passed.
**Completed task:** Release documentation and CI — added the required HA `2026.7.0`/`2026.7.2` Python 3.14 test matrix, Hassfest/HACS validation workflows, and README installation/configuration/privacy guidance in dedicated-repository commit `b17c73c`; `python -m pytest -q` — 173 passed.
**Completed task:** Release package-shape guard — added a bootstrap assertion that the repository contains exactly one integration directory, with focused verification in dedicated-repository commit `cfeb149`; `python -m pytest -q tests/components/larapaper_bridge/test_bootstrap.py` — 5 passed.
**Completed task:** Default branch and release validation — pushed the accumulated state through `f57d58a` to public `main`, added a provisional MIT repository `LICENSE` and fixed manifest ordering in `f80d53d` and `92b9c51`, and published `main` at `92b9c51f5591c7227df4346de6bcf2641137bbdc`; local `python -m pytest -q` — 182 passed; Test run `29168672541` passed for HA `2026.7.0` and `2026.7.2`; Hassfest/HACS run `29168672577` passed.
**Completed task:** Forward-only v1.0.2 security maintenance — rejected literal and percent-encoded dot segments in relative/root-relative image paths, added regression coverage, and bumped the manifest/bootstrap assertion in dedicated-repository commit `313e192` (rebased onto remote `main` `cc82f8b`); `python -m pytest -q` — 192 passed.

**Goal / product intent:** Ship one installable Home Assistant integration that exposes a native camera and safe diagnostics while proving that repeated Home Assistant reads are projections only: `async_camera_image()` never performs network, protocol, scheduling, conversion, or state advancement. Make the integration installable through a HACS custom repository on a clean HA instance, then publish and verify the forward-only v1.0.2 security maintenance release. Default HACS-list inclusion is optional and is not a release gate.

**Exact targets:** In `brettinternet/home-assistant-larapaper-bridge`, own `custom_components/larapaper_bridge/camera.py`, `custom_components/larapaper_bridge/diagnostics.py`, `custom_components/larapaper_bridge/manifest.json`, `custom_components/larapaper_bridge/brand/icon.png`, `custom_components/larapaper_bridge/brand/PROVENANCE.json`, `custom_components/larapaper_bridge/strings.json`, `custom_components/larapaper_bridge/translations/en.json`, root `hacs.json`, root `README.md`, `.github/workflows/test.yml`, `.github/workflows/validate.yml`, repository settings, and the forward-only `v1.0.2` release. Keep exactly one integration directory and standard layout. Root `hacs.json` contains only `name` and `homeassistant: "2026.7.0"`. Release `manifest.json` uses canonical repository/issue URLs, the settled integration metadata, version `1.0.2`, and no Pillow requirement.

The only brand image is original neutral `custom_components/larapaper_bridge/brand/icon.png`, exactly 256×256, with no HA, HACS, TRMNL, or Larapaper marks. `custom_components/larapaper_bridge/brand/PROVENANCE.json` contains `author`, ISO-8601 `created_at`, `method_or_source`, SPDX `license`, and lowercase hex `sha256` of the exact PNG bytes. README states HA `2026.7.0`, custom-repository installation, config fields/defaults, manual model/playlist assignment, camera/card usage, cache-only reads, safe diagnostics codes, privacy, upgrades/uninstall, issue URL, and that default HACS inclusion is not promised.

**Dependencies / order:** HA-HACS-01 supplies repository identity, config/Store, and manifest domain; HA-HACS-02 owns runtime lifecycle/setup retry; HA-HACS-03 owns cycle scheduling/cache/image retries; HA-HACS-04 exclusively owns image transport, validation, conversion session/executor/admission, final-stop cleanup, and typed outcomes. HA-HACS-05 consumes and verifies those contracts and adds no lifecycle, scheduler, retry, image, session, executor, admission, or shutdown implementation. Run release checks before clean-install/manual QA, then tag and publish the forward-only `v1.0.2` release; retain v1.0.0 and v1.0.1 unchanged.

**Scope and ownership:**

* `CameraEntity` owns only the HA-facing projection. `async_camera_image()` returns the currently published immutable PNG bytes or `None`; it performs no I/O, awaits no task, starts no retry, changes no cache/timestamp/error/availability field, and never advances Larapaper. Set the entity image content type to `image/png`. It is available only when the runtime projection is ready and the cached image age is strictly less than the configured freshness limit; cold, starting, stale, and error projections are unavailable. At exact monotonic-age equality with the limit, return `None`/unavailable. Repeated reads must be byte-identical and side-effect free.
* `diagnostics.py` implements `async_get_config_entry_diagnostics(hass, entry)` as a deterministic, JSON-serializable whitelist of the settled runtime projection: `status` (`ready`, `starting`, `stale`, or `error`), `ready`, `stale`, `last_success_at`, `last_success_age_seconds`, `last_error`, `next_display_at`, and `next_retry_at`. Preserve the settled precedence: fresh cache is `ready` even with a non-null error; otherwise expired cache is `stale`; otherwise a failure with no serveable image is `error`; otherwise `starting`. `last_error` is null or exactly one of `setup_auto_assign_disabled`, `setup_failed`, `display_failed`, `invalid_display_response`, `image_url_missing`, `image_fetch_failed`, `image_validation_failed`, `image_conversion_failed`, or `internal_error`; unknown failures collapse to `internal_error`. Expose no entry identifier, MAC, URL, query, credential, header, image bytes, raw exception, response body, Pillow text, converter stderr, or arbitrary exception text. Diagnostics reads perform no network or state mutation.
* Packaging owns exact metadata paths, canonical links, translations, README, original brand/provenance, repository settings, release notes, tag, and GitHub publication. Repository description is exactly `A Home Assistant integration for Larapaper displays.`; topics are exactly `home-assistant`, `hacs`, `larapaper`, and `trmnl`; do not claim default-list membership.
* `.github/workflows/test.yml` runs focused modules and the complete integration-repository suite on Python 3.14 against HA `2026.7.0` and `2026.7.2`. `.github/workflows/validate.yml` runs Hassfest and the official HACS Action. CI validates `hacs.json`, manifest URLs/version, one integration directory, brand path/dimensions, all provenance fields and recomputed SHA-256, `translations/en.json`, and clean package contents; every failure blocks release.

**Non-goals:** No lifecycle coordinator, setup retry, cycle scheduler, cache/image retry policy, image transport/validation/conversion, image session/connector, executor/admission, final-stop registration/shutdown, custom dashboard JavaScript, Generic Camera, external bridge endpoints, authentication server, Docker/Compose, multi-device support, automated model/playlist assignment, or default HACS-list submission. Do not create a second integration, compatibility alias, or recipes-repository implementation.

**Resolved decisions / assumptions (with evidence and rationale):**

1. The target is the dedicated public repository `brettinternet/home-assistant-larapaper-bridge`, not this recipes repository. Evidence: the shared project contract explicitly reserves that repository while the current checkout inspection shows only recipes/scripts and docs. Rationale: HACS scans standard integration content and a public repository; separating the bridge prevents accidental packaging of unrelated recipes.
2. Minimum HA is exactly `2026.7.0`. Evidence: shared contract and OraclePackaging; HA's official `2026.7.0` requirements pin Pillow 12.2.0. Rationale: this is the compatibility floor and makes a Pillow manifest dependency redundant.
3. Root `hacs.json` is intentionally minimal (`name`, `homeassistant` only), and standard layout means content remains under `custom_components/`. Evidence: HACS publish/integration guidance and OraclePackaging's validator rationale. Rationale: avoid unsupported metadata and ensure Hassfest/HACS validate the same package shape.
4. The brand is integration-local at `custom_components/larapaper_bridge/brand/icon.png`, 256x256, original and neutral with recorded provenance. Evidence: HA custom-integration brand-image guidance and OraclePackaging's note that validator `ba1100f` checks this path when content is not in root. Rationale: it avoids trademark/copyright drift and makes provenance auditable.
5. The current V1 gate is HACS custom-repository installation plus the real forward-only v1.0.2 GitHub release; v1.0.0 and v1.0.1 remain immutable historical releases, and default HACS inclusion is optional. Evidence: HACS publish/include guidance and the v1.0.2 release/install verification below. Rationale: users can install the reviewed security-maintenance artifact immediately while preserving release history.
6. Camera and diagnostics expose typed, redacted projections only. Evidence: Home Assistant CameraEntity and diagnostics APIs, plus the lifecycle/image contracts in HA-HACS-02 through HA-HACS-04. Rationale: read methods must remain safe under repeated HA polling and must not leak remote data or alter scheduling.

**Open questions resolved:** The sole release repository is `brettinternet/home-assistant-larapaper-bridge`. Release paths, description, topics, CI lanes/workflows, brand provenance fields, and SHA-256 check are exact above. HA-HACS-04 owns image resources and final-stop cleanup; HA-HACS-05 verifies without reimplementation. The current V1 distribution gate is a HACS custom-repository install and forward-only `v1.0.2` release; v1.0.0 and v1.0.1 remain immutable, and default-list inclusion is optional later.

**Acceptance:**

* A fresh public clone has exactly one integration, root minimal `hacs.json`, canonical manifest URLs, version `1.0.2`, no Pillow requirement, `strings.json` and `translations/en.json`, the exact brand/provenance files and fields, exact description/topics, and CI-confirmed PNG dimensions plus recomputed SHA-256 equality; v1.0.0 and v1.0.1 remain unchanged.
* Hassfest and the official HACS Action pass on the release commit; CI passes focused modules and the complete integration-repository suite in exact HA `2026.7.0` and `2026.7.2` lanes on Python 3.14.
* A clean HA instance (no prior integration files or state) adds `brettinternet/home-assistant-larapaper-bridge` as a HACS custom repository, installs it, restarts, completes config flow, and loads one integration without manual file copying. Picture Entity and Picture Glance render the native camera after a successful published image.
* Camera tests prove cache-only behavior, PNG content type, immutable repeated reads, `None` while cold/starting/stale/error, and unavailability at exact freshness-age equality. Instrumented reads prove no network/protocol/scheduler/executor/cache/timestamp/error mutation.
* Diagnostics tests prove the exact whitelist and status precedence, fresh-cache-plus-error behavior, exact stale equality, UTC/null timestamps, monotonic nonnegative ages, next-display/retry projections, every allowlisted error, unknown-error collapse to `internal_error`, deterministic repeated reads, forbidden-value redaction, and zero network/state mutation.
* Manual QA verifies successful image display, recovery after display/image failures, required manual model/playlist assignment, reload/unload/restart behavior, and no late completion mutation through the HA-HACS-02–04 contracts. Unload must return promptly; the test must not claim that Home Assistant process exit waits for a running executor worker.
* Repository settings use the exact description and four topics; both exact workflow files pass; README, issue link, release notes, tag `v1.0.2`, and published release are present; the custom-repository install succeeds from that release. Verify HA-HACS-04's domain session/executor/admission reuse, final-stop close/shutdown, and busy-to-`image_conversion_failed` behavior without reimplementing it. No default HACS inclusion is asserted.

**Verification recorded (2026-07-11):** Public repository `https://github.com/brettinternet/home-assistant-larapaper-bridge` at `92b9c51f5591c7227df4346de6bcf2641137bbdc`; Test `https://github.com/brettinternet/home-assistant-larapaper-bridge/actions/runs/29168672541` and Hassfest/HACS Action `https://github.com/brettinternet/home-assistant-larapaper-bridge/actions/runs/29168672577` pass; HACS custom-repository install produced v1.0.0 from commit `92b9c51`; fresh HA `2026.7.0` runtime QA completed with config flow success, camera diagnostics `ready: true`, `image/png` camera proxy response, `/api/display` → `/image.png`, and restart identity reuse without `/api/setup`; release `https://github.com/brettinternet/home-assistant-larapaper-bridge/releases/tag/v1.0.0` contains only the integration and matching brand/provenance bytes. Review-fix commit `b23f066` fixes four production findings and passes 138 focused plus 187 full local component tests, but published release evidence remains pinned to `92b9c51`.
**Verification recorded (2026-07-12):** v1.0.2 release `https://github.com/brettinternet/home-assistant-larapaper-bridge/releases/tag/v1.0.2` targets exact commit `313e1920d4eff2ee853ffcfeec8aeaab80e2490c`; Test run `29177490827` and Hassfest/HACS Validate run `29177490829` both pass on that SHA, while v1.0.0 remains `92b9c51` and v1.0.1 remains `639351d`.
**Clean-install evidence:** Fresh HA `2026.7.0` config installed HACS, added the public custom repository through HACS, displayed and downloaded v1.0.2, restarted, and loaded the installed package; HACS state recorded `installed_commit: 313e192`, `last_version: v1.0.2`, and `version_installed: v1.0.2`. Config flow created one Larapaper Bridge entry and one camera; the HA integration page displayed Version 1.0.2.
**Manual-QA evidence:** Mock model/playlist assignment persisted; healthy camera returned HTTP 200 `image/png` PNG bytes and diagnostics reported `ready`; injected display failure produced `display_failed`/camera unavailable and recovered to `ready`; injected image failure produced `image_fetch_failed`/camera unavailable and recovered to `ready`; HA restart preserved the same MAC with `setup_calls` unchanged at 1 and returned diagnostics `ready`/camera `idle`.
**Review finding (2026-07-12):** The v1.0.1 prefix-traversal defect was reproduced under HA 2026.7.0/yarl normalization and fixed in v1.0.2; the final release, official CI, clean HACS install, and manual QA now target the same exact commit.
**Review state:** Owner authorized the forward-only v1.0.2 security release on 2026-07-12; no credential values are recorded. All HA-HACS-05 acceptance gates pass on `313e1920d4eff2ee853ffcfeec8aeaab80e2490c`.
**Review marker:** reviewed: `313e1920d4eff2ee853ffcfeec8aeaab80e2490c`; verified: local 192-pass suite, official Test `29177490827`, official Hassfest/HACS `29177490829`, v1.0.2 release/install/manual-QA evidence above.
license approval: HA-HACS-05 — owner explicitly approved MIT; license blocker cleared.
**Last known evidence:** Current checkout inspection (2026-07-11) shows `docs/home-assistant-hacs.md`, recipes, scripts, and no bridge integration or release metadata. Official references used for this contract: HACS publishing start (`https://www.hacs.xyz/docs/publish/start/`), HACS integration structure (`https://www.hacs.xyz/docs/publish/integration/`), HACS inclusion (`https://www.hacs.xyz/docs/publish/include/`), Home Assistant custom integration manifest requirements (`https://developers.home-assistant.io/docs/creating_integration_manifest/#custom-integration-requirements`), HA custom brand images (`https://developers.home-assistant.io/docs/core/integration/brand_images/#custom-integrations`), and HA 2026.7.0 requirements (`https://github.com/home-assistant/core/blob/2026.7.0/requirements.txt`). OraclePackaging corroborates the minimal hacs.json, one-directory layout, local brand path/dimensions, no-Pillow decision, exact minimum/current CI lanes, and release/custom-repository gate.

**Additional resolved implementation detail:** The CI compatibility matrix is fixed at Python 3.14 with Home Assistant `2026.7.0` and `2026.7.2`, the latest release observed on 2026-07-11. Do not resolve a floating latest version during release; future upgrades change the explicit current pin through normal dependency maintenance.

**Biggest non-obvious risk:** HACS/Hassfest may inspect repository contents and canonical links differently between the initial public repository and the release commit; a passing local test is insufficient unless the actual public custom-repository install and both official actions pass on the exact tagged commit.

**Optional out-of-scope idea:** After v1.0.0, submit the repository for default HACS inclusion and document the submission/review state; inclusion is never retroactively required for this item.

**Next action:** HA-HACS-06 remains a separate rough-draft candidate; do not extend this V1 release gate.

### HA-HACS-06 — multi-device entries and per-device manual display refresh

**Status / start condition:** Available to begin. HA-HACS-05 is complete through the verified v1.0.2 release, so no unfinished prerequisite remains. Implement in `brettinternet/home-assistant-larapaper-bridge` as a post-V1 release; this checked-out repository is now that implementation repository and this document is its writable backlog.

**Goal / product intent:** Allow one Home Assistant instance to configure multiple independent Larapaper synthetic devices. Each device gets its own config entry, persisted identity and credentials, runtime, scheduler, cache, native camera, redacted diagnostics, and native **Refresh display** button. Manual refresh reuses the existing display/image pipeline without allowing concurrent `/api/display` calls, changing camera reads, leaking credentials, or permitting one device's work to mutate another.

**Target and reference disposition:** All named production paths already exist and are edited unless marked new. Edit `custom_components/larapaper_bridge/manifest.json`, `const.py`, `config_flow.py`, `storage.py`, `runtime.py`, `provisioning.py`, `scheduler.py`, `image.py`, `camera.py`, `diagnostics.py`, `__init__.py`, `strings.json`, and `translations/en.json`; edit the matching existing tests under `tests/components/larapaper_bridge/`. Create `custom_components/larapaper_bridge/button.py` and `tests/components/larapaper_bridge/test_button.py` as item-owned new files. Edit root `README.md` and the existing bootstrap/package assertions for the release-facing behavior. No referenced path is generated, stale, external, or supplied by another item.

**Dependencies / order:** HA-HACS-01–05 are complete and supply the V1 storage, lifecycle, scheduler, image-resource, camera, diagnostics, packaging, and test contracts. Execute tasks 06A then 06B. Tasks 06C and 06D may proceed independently after 06B, then run the item-wide regression and migration matrix. Preserve the V1 protocol, security, image-validation, redaction, and cache-only camera contracts unless this item explicitly replaces a single-device assumption.

**Task 06A — Device-scoped config entries and atomic identity-registry migration (acceptance 1–3):** Remove `single_config_entry` from the manifest, set config-flow `VERSION = 2`, and replace the constant domain unique ID with the canonical MAC, so one config entry represents exactly one device and duplicate MACs are rejected before entry creation. Keep the existing user form and per-device URL/poll/image settings. Title a pending entry with its MAC; after complete credentials are loaded or provisioned, update its title to `friendly_id` through `hass.config_entries.async_update_entry`. Replace the V1 payload at fixed Store key `larapaper_bridge` with version-2 registry payload `{"version": 2, "devices": {"AA:BB:CC:DD:EE:FF": <identity>}}`, where each value is exactly the existing version-1 pending or complete identity payload and the map key must equal its canonical `mac`; reject extra top-level/record fields, duplicate/noncanonical keys, and malformed records. One domain adapter and `asyncio.Lock` serialize every load-select-mutate-save transaction so concurrent flows, provisioning completions, migrations, and removals cannot lose another device's update. Before selecting a MAC, a flow loads the registry and subtracts configured entry unique IDs. An omitted-MAC flow reuses the lexicographically first unclaimed registry record, whether pending or complete, and generates a new CSPRNG MAC only when none exists; a supplied MAC reuses its matching unclaimed record or creates it, and aborts if configured elsewhere. Persist the complete registry mutation atomically before `async_create_entry`, so interruption recovery needs no flow-local token or second index. Add `async_migrate_entry`: validate the sole V1 payload and matching entry MAC, atomically rewrite the same Store key to the one-record version-2 registry, then update entry version/unique ID/title through `hass.config_entries.async_update_entry`. Migration retries accept an already-matching version-2 record and reject conflicting additional records, malformed state, or MAC mismatch without mutation. New V2 flows never interpret a version-1 payload; only migration may convert it. `async_remove_entry` atomically removes only that MAC's map record and preserves every other record; unload/reload never deletes state. Generated MACs are checked against both registry records and configured unique IDs and regenerated on collision.

**Task 06B — Per-entry runtime, entity, and diagnostic isolation (acceptance 4–5):** Refactor `RuntimeHolder.current` into an entry-ID-keyed runtime map and replace the single domain lifecycle counter with monotonically increasing epochs owned per entry ID. Creating, looking up, invalidating, unloading, and notifying one runtime must address that entry only; unloading one entry must not cancel another entry's tasks, timers, scheduler, image work, camera state writes, or provisioning. Keep lifecycle epoch and cycle generation separate within each runtime. Forward both `Platform.CAMERA` and the new `Platform.BUTTON`. Give the camera and button stable unique IDs derived from the canonical MAC, not `entry_id`, and attach both to one Home Assistant device-registry record identified by `(DOMAIN, MAC)` with the friendly ID as the display name and `Larapaper` as manufacturer. Diagnostics continue through the config-entry diagnostics API and resolve only the requested entry runtime. Preserve exact V1 status precedence, error allowlist, freshness boundary, redaction, and cache-only camera behavior.

**Task 06C — Fair shared image admission across devices (acceptance 6):** Keep one domain-scoped policy-aware image session, connector/resolver, `ThreadPoolExecutor(max_workers=1)`, and final-stop cleanup registration. Replace the V1 opportunistic nonblocking admission bit with a FIFO, cancellation-aware arbiter keyed by config entry ID. Queue only a lightweight admission request before image fetch/conversion; never queue or retain encoded image payloads. Permit at most one waiter per device and rotate admitted devices in FIFO order so a fast device cannot repeatedly overtake another. A stale lifecycle/cycle token, unload, or abandonment removes that device's waiter synchronously on the HA event loop. Once admitted, retain the V1 rule that the slot remains occupied until the underlying conversion future actually finishes, including logical timeout/cancellation. A worker that never returns may still stall all conversions, matching the documented V1 process-isolation limit, but must not stall any device's display cadence or allow cross-device publication.

**Task 06D — Coalesced manual display cycle and native button (acceptance 7–9):** Add `button.py` with one stateless `ButtonEntity` named **Refresh display** per entry. It is available only while that entry is current, provisioning is complete, and its scheduler is running; camera cold/stale/error state does not disable it. `async_press()` calls a scheduler-owned `async_request_refresh()` API and never calls the client or image operation directly. If no display call is active, cancel the pending automatic deadline, advance the cycle through the existing increment-then-abandon sequence, and start one immediate display call. If a display call is already active, coalesce the press into that call and return without queueing another; repeated concurrent presses have the same behavior. An admitted manual cycle uses the normal accepted/invalid/failed settlement rules, resets the next automatic deadline to settlement plus the selected effective interval, and uses the existing captured-URL image retry window. A timeout is `display_failed`, receives no fast retry, and is treated as an ambiguously completed display exactly like a scheduled timeout. Reuse the existing error allowlist; add no manual-only diagnostic code. Update translations and README with multi-device add/remove behavior, the button semantics, and the fact that manual refresh advances only that device's Larapaper playlist.

**Scope boundaries / non-goals:** No config subentries or multi-device data inside one config entry; no discovery; no playlist/model assignment; no image history; no several views for one device; no custom dashboard or direct frontend HTTP; no direct button-to-client call; no concurrent display calls for one device; no new Larapaper endpoint, credential shape, public route, retry code, or image format; no rollback to v1.0.2 after Store migration; no default-HACS-list requirement. Do not broaden private-origin SSRF exceptions or weaken redirect, byte, dimension, timeout, conversion, credential-redaction, or filesystem-free guarantees.

**Resolved decisions and evidence:**

1. Use repeated config entries, one per device. Home Assistant's config-entry lifecycle natively sets up, unloads, removes, and migrates entries independently; the current V1 runtime, camera, and diagnostics are already entry-shaped. Config subentries or one config entry containing a device map would retain a shared failure/removal boundary and require a second lifecycle model. The domain Store registry is persistence-only and does not combine config-entry lifecycles.
2. Use canonical MAC as config-entry unique ID and as the key inside one atomic domain identity registry. The MAC is already the persisted Larapaper identity and is available before entry creation. A single versioned map lets a restarted flow discover orphaned pre-entry pending state without a second non-atomic index; deterministic selection of the first unclaimed MAC is safe because an omitted-MAC flow has no user-selected identity to distinguish one orphan from another.
3. Migrate the fixed Store forward in place to a versioned map. Home Assistant calls `async_migrate_entry` for version changes and requires `async_update_entry` rather than direct mutation. One private atomic Store write avoids the crash gap between a per-device record and a discovery index; the adapter lock prevents concurrent read-modify-write loss. Device removal uses the documented `async_remove_entry` hook and removes only its map member.
4. Coalesce a press with an in-flight display call and reset cadence only when a new manual cycle is admitted. The active call already satisfies “get the next display now”; queueing another would advance twice, while rejection provides worse UX without increasing safety. Settlement anchoring preserves the established side-effect/timeout rule and prevents an immediate scheduled duplicate.
5. Keep the button available through cold, stale, and runtime error states after provisioning. Manual recovery is useful in those states; availability follows the ability to admit a scheduler request, not camera freshness. Provisioning/entry unload remain unavailable because credentials or lifecycle authority are absent.
6. Keep shared image resources but add fair pre-fetch admission. Separate executors would multiply non-killable threads across devices and reloads; queuing encoded payloads would multiply memory. A lightweight FIFO preserves one-worker bounds, prevents ordinary starvation, and keeps bytes out of the queue.
7. Stable entity/device identity uses MAC. The current camera uses `entry_id`, which is insufficient for migration/recreation stability; `(DOMAIN, MAC)` matches Home Assistant's device-registry identifier model and cannot collide across configured devices because config flow rejects duplicate MACs.

**Open questions resolved:** All seven draft questions are settled above: active display calls coalesce; admitted manual cycles reset cadence from settlement; timed-out manual calls use `display_failed` with no fast retry; devices use repeated config entries; the button remains available during cold/stale/error only after provisioning; Store migration is forward-only into one atomic device registry, interrupted flows deterministically reclaim unconfigured records, and removal deletes only the selected map member; shared conversion becomes FIFO-admitted before payload fetch. “Multiple devices” and “manual next display” remain one item because the button's stable identity, lifecycle lookup, and isolation depend on the multi-entry conversion, while tasks 06A–06D provide bounded implementation passes.

**Acceptance:**

1. **06A:** Config-flow tests create two entries with distinct canonical MAC unique IDs and independent settings, reject supplied/generated collisions before mutation, serialize concurrent flow transactions, and prove an interruption after registry save but before entry creation is recovered by a restarted omitted-MAC flow without generating a replacement. Multiple orphan records are reclaimed in canonical-MAC lexical order; configured records are never reclaimed.
2. **06A:** Storage/migration tests validate the exact version-2 registry and nested V1 record schemas, reject key/record MAC disagreement and extra fields, preserve unrelated records across concurrent provisioning writes, migrate complete and pending V1 payloads in place, update entry version/unique-ID/title, accept an already-matching retry, and fail closed on malformed/mismatched/conflicting state.
3. **06A:** Removal/restart tests prove removing one entry atomically removes only its MAC member from the registry, while unload/reload and Home Assistant restart preserve every remaining identity and complete credentials without another setup call. A removal racing provisioning cannot resurrect the removed record or discard another device's update.
4. **06B:** Two loaded entries provision and schedule independently; unloading/reloading either one leaves the other's epoch, generation, tasks, timers, display calls, cache, availability, and diagnostics unchanged. Late completions cannot publish or persist into either the wrong entry or a replacement lifecycle.
5. **06B:** Camera/button registry tests prove stable MAC-derived unique IDs, one device-registry record per MAC, friendly names, exact per-entry diagnostics lookup, byte-identical cache-only camera reads, stale equality, and zero network/state mutation from camera or diagnostics reads.
6. **06C:** Image tests prove one shared session/executor, FIFO admission across at least three devices, one lightweight waiter per device, no queued encoded payload, cancellation/unload removal, no starvation under repeated requests, admission held through a cancellation-ignoring worker, unaffected display cadence, and token-gated publication to the owning runtime only.
7. **06D:** Scheduler/button tests prove an idle press starts exactly one immediate cycle, concurrent presses coalesce, a press during an active scheduled/manual display adds no call, no two display calls overlap, and button code never invokes the client directly.
8. **06D:** Fake-clock tests prove admitted manual success/invalid/failure/timeout settlement resets the automatic deadline exactly once using the normal interval fallback, performs no fast display retry, preserves captured-URL-only image retries, and abandons prior image work using increment-before-await token ordering.
9. **06D:** Availability and end-to-end tests prove the button is unavailable before provisioning/after unload but available while camera state is cold, stale, or error; pressing advances only the selected mock device; the other device's call count, cadence, cache, diagnostics, and playlist remain unchanged. README/translations describe these exact semantics.

**Pending verification:** Run the focused config-flow/storage/migration tests after 06A; runtime/camera/diagnostics tests after 06B; image tests after 06C; scheduler/button tests after 06D. Then run `python -m pytest -q tests/components/larapaper_bridge`, both supported HA CI lanes, Hassfest, and the official HACS Action. Manual QA must add two devices, restart, press each button during idle and an in-flight cycle, remove one entry, and confirm the remaining camera/credentials/cadence survive. Publish or version-bump only after those gates; this refinement does not choose the next release number.

**Last known evidence:** v1.0.2 at `313e1920d4eff2ee853ffcfeec8aeaab80e2490c` passed 192 local tests, both official CI workflows, clean HACS installation, restart identity reuse, camera/diagnostics QA, and display/image failure recovery. Repository inspection shows the implementation paths above now exist here: V1 has one fixed Store key, one `RuntimeHolder.current`, one lifecycle counter, one camera platform, entry-ID-derived camera unique ID, one domain image executor/admission slot, and manifest `single_config_entry: true`. Those are the exact single-device seams replaced by 06A–06D; no missing implementation reference remains.

**Biggest non-obvious risk:** A “multi-device” refactor that changes the Store schema but leaves a singleton runtime lookup—or performs unlocked registry read-modify-write—can pass isolated tests while unloading one entry cancels another or one provisioning completion discards another device's record. Every lookup, callback, timer, entity, diagnostics read, and token check must carry the owning entry ID and MAC explicitly, and every registry mutation must hold the shared adapter lock through its atomic save.

**Optional out-of-scope idea:** A later release may add reconfiguration, discovery, or per-device image-worker quotas. None is required to add/remove independent entries and safely request a manual display cycle.

**Next action:** Implement Task 06A against the existing V1 config flow and Store tests, commit it as one migration/config-entry outcome, then continue in the recorded task order.
