"""Bootstrap checks for the integration package layout."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).parents[3]
INTEGRATION = ROOT / "custom_components" / "larapaper_bridge"


def test_repository_contains_one_integration() -> None:
    integration_dirs = sorted(
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and not path.name.startswith("__")
    )
    assert integration_dirs == ["larapaper_bridge"]


def test_manifest_and_translation_resources() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert manifest == {
        "domain": "larapaper_bridge",
        "name": "Larapaper Bridge",
        "version": "1.0.3",
        "documentation": "https://github.com/brettinternet/home-assistant-larapaper-bridge",
        "issue_tracker": "https://github.com/brettinternet/home-assistant-larapaper-bridge/issues",
        "codeowners": ["@brettinternet"],
        "config_flow": True,
        "integration_type": "device",
        "iot_class": "local_polling",
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


def test_release_documentation_and_workflows() -> None:
    readme = (ROOT / "README.md").read_text()
    for required_text in (
        "2026.7.0",
        "Custom repositories",
        "Minimum poll seconds",
        "manual",
        "cache-only",
        "setup_auto_assign_disabled",
        "Privacy and network behavior",
        "Upgrades and uninstall",
        "https://github.com/brettinternet/home-assistant-larapaper-bridge/issues",
    ):
        assert required_text in readme

    test_workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert 'python-version: "3.14"' in test_workflow
    assert '"2026.7.0"' in test_workflow
    assert '"2026.7.2"' in test_workflow
    assert "test_bootstrap.py" in test_workflow
    assert "python -m pytest -q" in test_workflow

    validation_workflow = (
        ROOT / ".github" / "workflows" / "validate.yml"
    ).read_text()
    assert "home-assistant/actions/hassfest@master" in validation_workflow
    assert "hacs/action@main" in validation_workflow
    assert "category: integration" in validation_workflow


def test_declared_python_modules_import() -> None:
    for path in INTEGRATION.glob("*.py"):
        importlib.import_module(f"custom_components.larapaper_bridge.{path.stem}")
