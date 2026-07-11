"""Bootstrap checks for the integration package layout."""

from __future__ import annotations

import importlib
import json
from pathlib import Path


ROOT = Path(__file__).parents[3]
INTEGRATION = ROOT / "custom_components" / "larapaper_bridge"


def test_manifest_and_translation_resources() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest == {
        "domain": "larapaper_bridge",
        "name": "Larapaper Bridge",
        "version": "0.1.0",
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


def test_declared_python_modules_import() -> None:
    for path in INTEGRATION.glob("*.py"):
        importlib.import_module(f"custom_components.larapaper_bridge.{path.stem}")
