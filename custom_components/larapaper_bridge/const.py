"""Constants for the Larapaper Bridge integration."""

DOMAIN = "larapaper_bridge"
CONF_BASE_URL = "base_url"
CONF_IMAGE_BASE_URL = "image_base_url"
CONF_MAC = "mac"
CONF_MIN_POLL_SECONDS = "min_poll_seconds"
CONF_MAX_STALE_SECONDS = "max_stale_seconds"
CONF_MAX_IMAGE_BYTES = "max_image_bytes"

STORE_VERSION = 1
STORE_KEY = DOMAIN
IDENTITY_VERSION = 1

DEFAULT_MIN_POLL_SECONDS = 60.0
DEFAULT_MAX_STALE_SECONDS = 3600
DEFAULT_MAX_IMAGE_BYTES = 10_485_760

ERROR_INVALID_BASE_URL = "invalid_base_url"
ERROR_INVALID_IMAGE_BASE_URL = "invalid_image_base_url"
ERROR_INVALID_MAC = "invalid_mac"
ERROR_INVALID_MIN_POLL_SECONDS = "invalid_min_poll_seconds"
ERROR_INVALID_MAX_STALE_SECONDS = "invalid_max_stale_seconds"
ERROR_INVALID_MAX_IMAGE_BYTES = "invalid_max_image_bytes"
ERROR_INVALID_STORED_STATE = "invalid_stored_state"
ERROR_MAC_MISMATCH = "mac_mismatch"
