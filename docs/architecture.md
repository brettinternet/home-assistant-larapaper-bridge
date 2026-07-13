# Larapaper Bridge: architecture and operations

This repository ships a Home Assistant custom integration for self-hosted Larapaper displays. HACS installs the package from the public `brettinternet/home-assistant-larapaper-bridge` repository.

## Production direction

- Home Assistant is the network boundary. It talks directly to Larapaper and to returned image hosts.
- The product is a native integration: config flow, per-device runtime, scheduler, image pipeline, camera, diagnostics, and **Refresh display** button.
- Supported Home Assistant floor: `2026.7.0`. CI also exercises `2026.7.2` on Python 3.14.
- One config entry represents one device. Model and playlist assignment remain manual in Larapaper administration.
- HACS custom-repository installation is supported; default HACS-list inclusion is not promised.
- The former Bun bridge, Generic Camera setup, public image/health routes, dashboard JavaScript, container deployment, and disk image cache are not part of this product.

## Runtime boundaries

```text
Config flow
  -> atomic identity registry
  -> per-entry provisioning/runtime
  -> settlement-anchored display scheduler
  -> bounded image pipeline
  -> immutable in-memory PNG cache
  -> native camera, diagnostics, and Refresh display button
```

Ownership is intentionally split:

- `config_flow.py` validates URLs, MACs, and numeric settings without network I/O.
- `storage.py` owns the private atomic Home Assistant Store and versioned identity registry.
- `client.py` owns one-shot `/api/setup` and `/api/display` protocol calls.
- `provisioning.py` owns setup retries and pending/complete credential transitions.
- `runtime.py` owns per-entry lifecycle state, task registration, and fencing.
- `scheduler.py` owns display cadence, cache publication, image retries, and manual refresh coalescing.
- `image.py` owns image transport, SSRF policy, validation/conversion, shared admission, and final-stop resources.
- `camera.py`, `diagnostics.py`, and `button.py` expose projections or scheduler requests; they do not own protocol calls.

## Configuration and identity

The config flow accepts:

- `base_url`: trimmed absolute HTTP(S), with credentials, query, and fragment rejected; pathname prefixes are preserved.
- `image_base_url`: optional HTTP(S) origin. Its path, query, credentials, and fragment are rejected; only scheme, host, and effective port override a returned image URL.
- `mac`: optional six-octet hexadecimal address, stored uppercase and canonical. Blank generates a CSPRNG-backed locally administered, unicast MAC.
- `min_poll_seconds`: finite positive number, default `60`.
- `max_stale_seconds`: positive integer, default `3600`.
- `max_image_bytes`: positive integer, default `10485760`.

The domain-wide Store uses private atomic writes:

```python
Store(hass, 1, "larapaper_bridge", private=True, atomic_writes=True)
```

The payload is a version-2 registry:

```json
{
  "version": 2,
  "devices": {
    "AA:BB:CC:DD:EE:FF": {
      "version": 1,
      "mac": "AA:BB:CC:DD:EE:FF"
    }
  }
}
```

A device identity is either pending (`version`, `mac`) or complete (`version`, `mac`, `api_key`, `friendly_id`). Extra fields and mismatched MAC keys are invalid. Registry read-modify-write operations hold the shared adapter lock through the atomic save.

The flow claims a MAC before entry creation, persists pending identity before setup, and reuses orphaned pending state after interruption. Setup persists complete credentials only after a successful response. Config-entry migration converts the V1 single-identity payload in place; removal deletes only the selected registry member. Malformed state fails closed and is never silently deleted or reprovisioned.

## Protocol and scheduling invariants

- `/api/setup` sends exactly `ID`; `/api/display` sends exactly `ID` and `Access-Token`.
- API redirects are refused. Each operation has one 10-second budget covering headers and body consumption.
- Setup failures retry at `+5`, `+10`, `+20`, `+40`, then repeated `+60` seconds while the entry remains loaded. Cancellation and Store errors are not converted into client failures.
- The first display cycle starts immediately. Every accepted, invalid, or failed display call settles before the next deadline is calculated.
- Scheduling uses the event loop's monotonic clock: `settlement + effective_interval`, where `effective_interval = max(refresh_rate, min_poll_seconds)`.
- Display failures have no fast retry because the side effect may be ambiguous. Invalid rates preserve the last valid interval or the configured minimum. Missing/empty image URLs preserve the cache and create no image retry.
- Image failures retry only the captured resolved URL at `+5/+10/+20/+40/+60` seconds, repeating `+60`, and only when the retry is strictly before the next display deadline.

Each runtime carries two independent tokens:

- `lifecycle_epoch`: changes only when that config entry unloads or invalidates.
- `cycle_generation`: increments at each display deadline before old work is cancelled or abandoned.

Every asynchronous completion checks the applicable token on the Home Assistant event loop before changing state. Unload marks the entry stopped, increments its epoch, cancels timers/tasks, abandons image work, and returns without waiting for a blocked worker. A late completion cannot publish cache data, credentials, diagnostics, availability, or retry state.

## Image security and resource bounds

Image URLs are resolved against the configured Larapaper origin. The optional image origin changes only authority; source path and query remain intact.

- Only HTTP(S) URLs without credentials or fragments are accepted.
- Private destinations are allowed only for the exact configured Larapaper or image-base origin. Other destinations must resolve exclusively to global-unicast addresses.
- Mixed public/private DNS answers fail closed. Resolution policy is enforced at connection time and repeated for every redirect to prevent DNS rebinding bypasses.
- Redirects are manual: only `301`, `302`, `303`, `307`, and `308`, at most three hops, no malformed/missing locations, no HTTPS downgrade, and one shared 10-second budget.
- Image hosts receive no Larapaper headers, API keys, authorization, cookies, or other source-origin credentials.
- `Content-Length` and streamed bytes enforce the configured inclusive encoded-size limit. Missing or `application/octet-stream` media types use magic bytes; declared `image/png` and `image/bmp` must match; other types fail.
- PNG/BMP dimensions are checked before Pillow. Maximum dimension is `8192` per axis and maximum decoded pixels are `16,777,216`. Truncation, invalid dimensions, decompression-bomb warnings/errors, and converted-output overflow fail safely.
- Valid PNG bytes pass through. BMP is converted to PNG with Pillow. No image bytes reach disk.

One domain-scoped image session, one-worker executor, and cancellation-aware FIFO admission are shared across entries and reloads. Admission queues only lightweight requests, never encoded payloads. A timeout, cancellation, unload, or cycle replacement abandons work but does not release admission until the underlying future completes. Final-stop cleanup closes the session and shuts down the executor without waiting for a running worker.

## Entities and diagnostics

Each config entry has a MAC-derived camera and button identity attached to one Home Assistant device-registry record `(DOMAIN, MAC)`. Runtime lookup and lifecycle fencing are entry-ID scoped; unloading one entry cannot affect another.

The camera is cache-only:

- returns immutable last-good PNG bytes when fresh;
- returns `None` at cold start or when age is greater than or equal to `max_stale_seconds`;
- never calls `/api/display`, fetches an image, starts a retry, or mutates scheduler state;
- always reports `image/png`.

Diagnostics are a deterministic, JSON-serializable whitelist: `status`, `ready`, `stale`, `last_success_at`, `last_success_age_seconds`, `last_error`, `next_display_at`, and `next_retry_at`. Status precedence is `ready`, then `stale`, then `error`, then `starting`. Unknown errors collapse to `internal_error`; raw exceptions, URLs, response bodies, headers, credentials, Pillow errors, and converter output are never exposed.

**Refresh display** is available after provisioning while the entry scheduler is running, including cold, stale, and error camera states. A press requests a scheduler cycle; it never calls the client or image operation directly. Presses during an active display cycle coalesce, so one device never has concurrent `/api/display` calls. A manual cycle follows the normal settlement, cache, and captured-URL retry rules.

## Release and verification

A release must retain the standard package shape: one `custom_components/larapaper_bridge` integration, canonical manifest links/version, minimal root `hacs.json`, translations, and the integration-local 256×256 brand asset with recorded provenance. Do not add a Pillow manifest requirement while the supported Home Assistant floor supplies Pillow.

Run the focused and complete integration tests before release:

```text
mise exec python -- python -m pytest -q -W error tests/components/larapaper_bridge
```

Also run the managed Ruff and production Pyright checks. CI must pass both Home Assistant lanes, Hassfest, and the official HACS Action. Manual release QA covers clean HACS installation, config flow, restart identity reuse, model/playlist assignment, healthy camera output, display/image failure recovery, multi-device isolation, manual refresh coalescing, and prompt unload with blocked conversion work.

## References

- [Home Assistant integration structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [Home Assistant config flows](https://developers.home-assistant.io/docs/core/integration/config_flow/)
- [Home Assistant camera entities](https://developers.home-assistant.io/docs/core/entity/camera/)
- [Home Assistant data fetching](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [HACS integration publishing](https://www.hacs.xyz/docs/publish/integration/)
- [Repository README](../README.md)
