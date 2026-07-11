"""Bootstrap checks for the integration package layout."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).parents[3]
INTEGRATION = ROOT / "custom_components" / "larapaper_bridge"


def test_manifest_and_translation_resources() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest == {
        "domain": "larapaper_bridge",
        "name": "Larapaper Bridge",
        "version": "1.0.0",
        "documentation": "https://github.com/brettinternet/home-assistant-larapaper-bridge",
        "issue_tracker": "https://github.com/brettinternet/home-assistant-larapaper-bridge/issues",
        "codeowners": ["@brettinternet"],
        "config_flow": True,
        "integration_type": "device",
        "iot_class": "local_polling",
        "single_config_entry": True,
    }
    json.loads((INTEGRATION / "strings.json").read_text())
    json.loads((INTEGRATION / "translations" / "en.json").read_text())


def test_hacs_metadata_and_brand_provenance() -> None:
    assert json.loads((ROOT / "hacs.json").read_text()) == {
        "name": "Larapaper Bridge",
        "homeassistant": "2026.7.0",
    }

    icon_path = INTEGRATION / "brand" / "icon.png"
    with Image.open(icon_path) as icon:
        assert icon.size == (256, 256)
        icon.verify()

    provenance = json.loads((INTEGRATION / "brand" / "PROVENANCE.json").read_text())
    assert set(provenance) == {
        "author",
        "created_at",
        "method_or_source",
        "license",
        "sha256",
    }
    assert provenance["author"] == "brettinternet"
    assert provenance["license"] == "CC0-1.0"
    assert len(provenance["sha256"]) == 64
    assert provenance["sha256"] == hashlib.sha256(icon_path.read_bytes()).hexdigest()


def test_declared_python_modules_import() -> None:
    for path in INTEGRATION.glob("*.py"):
        importlib.import_module(f"custom_components.larapaper_bridge.{path.stem}")
