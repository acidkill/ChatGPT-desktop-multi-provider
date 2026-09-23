import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import patch_chatgpt_providers_linux as linux
from patch_chatgpt_providers import (
    PatchError,
    ensure_provider_config,
    validate_provider_config,
)


class ProviderConfigTests(unittest.TestCase):
    def test_default_provider_config_is_valid(self):
        validate_provider_config(linux.DEFAULT_PROVIDER_CONFIG)

    def test_linux_defaults_include_openrouter_models(self):
        config = linux.DEFAULT_PROVIDER_CONFIG
        providers = {item["id"] for item in config["providers"]}
        self.assertEqual(providers, {"openai", "openrouter"})
        self.assertEqual(config["model_providers"], {
            "openrouter/free": "openrouter",
        })

    def test_provider_model_metadata_matches_requested_capabilities(self):
        catalog = {"models": [{"slug": "gpt-5.6-sol", "priority": 1}]}
        models = {item["slug"]: item for item in linux.provider_models(catalog)}
        self.assertEqual(models["openrouter/free"]["context_window"], 200_000)
        self.assertEqual(models["openrouter/free"]["input_modalities"], ["text", "image"])
        self.assertEqual(models["glm-5.3"]["context_window"], 1_000_000)
        self.assertEqual(models["glm-5.3"]["input_modalities"], ["text"])
        self.assertEqual(models["glm-5.3-flash"]["input_modalities"], ["text", "image"])
        self.assertIn("slow prefill", models["nex"]["description"])

    def test_unknown_provider_mapping_is_rejected(self):
        config = json.loads(json.dumps(linux.DEFAULT_PROVIDER_CONFIG))
        config["model_providers"]["example/model"] = "missing"
        with self.assertRaises(PatchError):
            validate_provider_config(config)

    def test_existing_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop-model-providers.json"
            original = json.loads(json.dumps(linux.DEFAULT_PROVIDER_CONFIG))
            original["providers"][1]["label"] = "My Router"
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(ensure_provider_config(path, overwrite=False), "kept")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_linux_defaults_merge_into_existing_config_without_replacing_custom_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop-model-providers.json"
            original = {
                "version": 1,
                "default_provider": "openrouter",
                "providers": [
                    {"id": "openai", "label": "OpenAI", "description": "existing"},
                    {"id": "openrouter", "label": "My Router", "description": "custom"},
                ],
                "model_providers": {"openrouter/free": "openai", "my/model": "openrouter"},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(ensure_provider_config(
                path, overwrite=False, default_config=linux.DEFAULT_PROVIDER_CONFIG,
                merge_defaults=True,
            ), "merged")
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["default_provider"], "openrouter")
            self.assertEqual(result["providers"][1]["label"], "My Router")
            self.assertEqual(result["model_providers"]["openrouter/free"], "openai")
            self.assertEqual(result["model_providers"]["my/model"], "openrouter")

    def test_malformed_existing_config_is_rejected_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop-model-providers.json"
            original = "{broken"
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(PatchError):
                ensure_provider_config(path, overwrite=False)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_unmapped_model_uses_default_provider(self):
        config = json.loads(json.dumps(linux.DEFAULT_PROVIDER_CONFIG))
        config["model_providers"].pop("glm-5.3")
        validate_provider_config(config)
        self.assertEqual(config["default_provider"], "openai")


class PackageDetectionTests(unittest.TestCase):
    def test_supported_package_version_is_read_from_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "resources/linux-package-metadata.json"
            metadata.parent.mkdir()
            metadata.write_text(json.dumps({"version": linux.MINIMUM_VERSION}), encoding="utf-8")
            self.assertEqual(linux.package_version(Path(directory)), linux.MINIMUM_VERSION)

    def test_minimum_and_newer_versions_are_accepted(self):
        linux.require_supported_version(linux.MINIMUM_VERSION)
        linux.require_supported_version("26.916.10000")
        linux.require_supported_version("27.0.0")

    def test_older_or_unparseable_versions_are_rejected(self):
        for version in ("26.915.31944", "26.914.99999", "latest"):
            with self.subTest(version=version), self.assertRaises(PatchError):
                linux.require_supported_version(version)

    def test_missing_package_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(PatchError):
            linux.package_version(Path(directory))


class CodexConfigurationTests(unittest.TestCase):
    def test_configure_codex_merges_provider_models_without_overwriting_existing_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "package"
            binary = source / "resources/codex"
            binary.parent.mkdir(parents=True)
            catalog = {"models": [{"slug": "gpt-5.6-sol", "priority": 1}]}
            binary.write_text(
                "#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(catalog)) + ")\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            old_catalog = codex_home / "my-models.json"
            old_catalog.write_text(json.dumps({"models": [{"slug": "my-existing-model"}]}))
            config = codex_home / "config.toml"
            config.write_text(
                f'model_catalog_json = "{old_catalog}"\n\n[profiles.work]\nmodel = "gpt-5.6-sol"\n',
                encoding="utf-8",
            )

            with patch.object(linux.shutil, "which", return_value="/usr/bin/secret-tool"):
                config_path, catalog_path = linux.configure_codex(source, codex_home)
                original_config = config.read_text()
                original_catalog = catalog_path.read_text()
                linux.configure_codex(source, codex_home)

            self.assertEqual(config_path, config)
            self.assertEqual(config.read_text(), original_config)
            self.assertEqual(catalog_path.read_text(), original_catalog)
            self.assertEqual(json.loads(old_catalog.read_text())["models"][0]["slug"], "my-existing-model")
            generated = json.loads(catalog_path.read_text())
            slugs = {item["slug"] for item in generated["models"]}
            self.assertTrue({"my-existing-model", "openrouter/free"}.issubset(slugs))
            updated = config.read_text()
            self.assertIn("[model_providers.openrouter]", updated)
            self.assertIn(str(catalog_path), updated)
            self.assertEqual(os.stat(catalog_path).st_mode & 0o777, 0o600)

    def test_invalid_existing_codex_toml_fails_without_changing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "package"
            binary = source / "resources/codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text("[broken\n", encoding="utf-8")
            with patch.object(linux.shutil, "which", return_value="/usr/bin/secret-tool"), self.assertRaises(PatchError):
                linux.configure_codex(source, codex_home)
            self.assertEqual(config.read_text(), "[broken\n")
            self.assertFalse((codex_home / "model-catalogs" / linux.MODEL_CATALOG_NAME).exists())


class LinuxPatchTests(unittest.TestCase):
    def test_supported_anchors_are_patched_once(self):
        central = """const X = class {
        async sendRequest(e, t, n) {
          return e === `config/read`
            ? this.sendConfigReadRequest(t, n)
            : this.enqueueRequest(e, t, n);
        }
        async sendConfigReadRequest(e, t) { return e; }
        async prewarmThreadStart(e, t) {
          if (this.dispatchMessage == null) throw Error(`missing`);
        }
        }
"""
        picker = '''const x = '[data-model-selected="true"]';
const modelOptionsDisabled = false;
'''
        with tempfile.TemporaryDirectory() as directory:
            central_path = Path(directory) / "app-initial.js"
            picker_path = Path(directory) / "app-primary.js"
            central_path.write_text(central, encoding="utf-8")
            picker_path.write_text(picker, encoding="utf-8")
            linux.patch_bundles(central_path, picker_path)
            first = central_path.read_text(encoding="utf-8")
            linux.patch_bundles(central_path, picker_path)
            self.assertEqual(first, central_path.read_text(encoding="utf-8"))
            self.assertEqual(first.count(linux.CENTRAL_MARKER), 1)
            self.assertEqual(picker_path.read_text(encoding="utf-8").count(linux.PICKER_MARKER), 2)

    def test_ambiguous_anchor_fails_without_editing_files(self):
        central = """const X = class {
        async sendRequest(e, t, n) {}
        async sendRequest(e, t, n) {}
        async sendConfigReadRequest(e, t) {}
        async prewarmThreadStart(e, t) { if (this.dispatchMessage == null) {} }
        }
"""
        picker = '''const x = '[data-model-selected="true"]'; const modelOptionsDisabled = false;'''
        with tempfile.TemporaryDirectory() as directory:
            central_path = Path(directory) / "app-initial.js"
            picker_path = Path(directory) / "app-primary.js"
            central_path.write_text(central, encoding="utf-8")
            picker_path.write_text(picker, encoding="utf-8")
            with self.assertRaises(PatchError):
                linux.patch_bundles(central_path, picker_path)
            self.assertEqual(central_path.read_text(encoding="utf-8"), central)
            self.assertEqual(picker_path.read_text(encoding="utf-8"), picker)

    def test_launcher_preserves_arguments_and_wayland(self):
        text = linux.launcher_text(Path("/tmp/codex copy"))
        self.assertIn("--ozone-platform=wayland", text)
        self.assertIn('"$@"', text)
        self.assertIn("'/tmp/codex copy'", text)


if __name__ == "__main__":
    unittest.main()
