# Larapaper Bridge

Larapaper Bridge is a Home Assistant custom integration for self-hosted Larapaper displays. Each config entry provisions one synthetic device, polls its display on the configured cadence, and exposes the latest safe image as a native Home Assistant camera.

## Requirements

- Home Assistant `2026.7.0` or newer.
- A Larapaper server reachable from Home Assistant.
- A Larapaper account allowed to assign new devices, unless the device is assigned manually before setup.

This repository is a HACS custom repository. It is public, but it is not currently promised for the default HACS list.

## Installation through HACS

1. Open **HACS → Integrations** in Home Assistant.
2. Open the three-dot menu, choose **Custom repositories**, and add `https://github.com/brettinternet/home-assistant-larapaper-bridge` with category **Integration**.
3. Install **Larapaper Bridge** and restart Home Assistant.
4. Open **Settings → Devices & services**, choose **Add integration**, and select **Larapaper Bridge**.

Do not copy files into Home Assistant manually when installing from the custom repository. HACS manages upgrades and removal.

## Configuration

The config flow accepts these fields:

| Field | Default | Behavior |
| --- | --- | --- |
| Larapaper base URL | required | Absolute HTTP(S) URL. Credentials, query strings, and fragments are rejected. A pathname prefix is preserved. |
| Image base URL | blank | Optional HTTP(S) origin used for returned image hosts. Its path, query, and credentials are not accepted. |
| Device MAC address | blank | Six colon-delimited hexadecimal octets. Blank generates a stable locally administered, unicast MAC. |
| Minimum poll seconds | `60` | Finite positive number. The effective display interval never falls below this value. |
| Maximum stale seconds | `3600` | Positive integer freshness limit for the camera cache. |
| Maximum image bytes | `10485760` | Positive integer encoded-image limit. |

The integration stores the synthetic identity and Larapaper credentials in Home Assistant-managed private state. It does not make network requests while the config form is being validated.

After provisioning, assign the device's model and playlist in the Larapaper administration UI. Model and playlist assignment is intentionally manual; this integration does not select or edit them.

## Camera cards

The integration creates one native camera entity per config entry. Add it to a dashboard with **Picture Entity**, **Picture Glance**, or another built-in camera card. Use the entity picker to select the Larapaper camera; entity IDs can vary with Home Assistant's naming rules.

Example:

```yaml
type: picture-entity
entity: camera.larapaper_bridge
camera_view: auto
show_name: true
show_state: false
```

Camera reads are cache-only projections. `async_camera_image()` never calls Larapaper, fetches an image, starts a retry, advances the playlist, or mutates scheduler state. Repeated dashboard and camera-proxy refreshes therefore do not create additional `/api/display` calls or image-host requests.

## Multiple devices and manual refresh

Add one config entry for each Larapaper device. Each entry owns its identity, credentials, scheduler, cache, camera, diagnostics, and **Refresh display** button. Removing an entry removes only that device's integration state; the other entries continue using their own cameras and schedules.

The **Refresh display** button is available after the entry finishes provisioning and its scheduler is running, including while the camera cache is cold, stale, or in an error state. A press starts one immediate display cycle when none is active. Presses during an active cycle coalesce instead of starting another `/api/display` call. Manual cycles use the normal display settlement, image-retry, and failure rules. A manual refresh advances only the selected device's Larapaper playlist.

## Diagnostics and status

Home Assistant diagnostics expose only a fixed, redacted state projection:

- `status`: `ready`, `starting`, `stale`, or `error`
- `ready`, `stale`
- `last_success_at`, `last_success_age_seconds`
- `last_error`
- `next_display_at`, `next_retry_at`

The allowlisted error codes are:

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

Diagnostics contain no API key, URL, query, header, response body, exception text, or converter output. A fresh cached image remains `ready` even when the most recent cycle recorded an error.

## Privacy and network behavior

- Larapaper `ID` and `Access-Token` headers are sent only to Larapaper API requests.
- Returned image hosts receive no Larapaper credentials, Home Assistant credentials, cookies, or authorization headers.
- Home Assistant's authenticated camera proxy protects dashboard access.
- Images are validated and converted in memory; image bytes are never written to disk.
- Secrets never appear in entity state, diagnostics, logs, URLs, or test output.

## Upgrades and uninstall

Use HACS to upgrade the integration, then restart Home Assistant when prompted. Before uninstalling, remove the **Larapaper Bridge** config entry from **Settings → Devices & services**. Home Assistant owns the integration's private stored identity and credentials; no image cache is left on disk.

## Support

Report bugs and security-relevant behavior at [github.com/brettinternet/home-assistant-larapaper-bridge/issues](https://github.com/brettinternet/home-assistant-larapaper-bridge/issues). Include the safe diagnostics projection and Home Assistant version, but never include API keys, raw URLs with credentials, or response bodies.
