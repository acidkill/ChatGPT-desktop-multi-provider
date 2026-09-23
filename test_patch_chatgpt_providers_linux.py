import json
from pathlib import Path
import tempfile
import unittest

import patch_chatgpt_providers_linux as linux
from patch_chatgpt_providers import PatchError, ensure_provider_config, validate_provider_config


class ProviderConfigTests(unittest.TestCase):
    def test_default_provider_config_is_valid(self):
        validate_provider_config(linux.DEFAULT_PROVIDER_CONFIG)

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
        config["model_providers"].pop("moonshotai/kimi-k3")
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
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PatchError):
                linux.package_version(Path(directory))


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
