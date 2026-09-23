#!/usr/bin/env python3
"""Install the provider picker into a per-user copy of the Linux Codex app.

The system package is read as the source and is never modified. JavaScript
patches are version-sensitive and the install copy is swapped only after ASAR
packing and marker checks succeed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10 remain usable for the macOS installer.
    tomllib = None
from typing import Any

from patch_chatgpt_providers import (
    PatchError,
    atomic_write_json,
    ensure_provider_config,
    run,
    validate_provider_config,
)

MINIMUM_VERSION = "26.915.31945"
PATCH_MARKER = "__codexDesktopModelProvidersLinuxV1"
CENTRAL_MARKER = PATCH_MARKER + "Central"
PICKER_MARKER = PATCH_MARKER + "Picker"
ASAR_PACKAGE = "@electron/asar@3.2.10"
PRETTIER_PACKAGE = "prettier@3.6.2"
DEFAULT_SOURCE = Path("/usr/lib/chatgpt")
DEFAULT_INSTALL = Path.home() / ".local/opt/chatgpt-provider-patched"
DEFAULT_LAUNCHER = Path.home() / ".local/bin/chatgpt-providers"
DEFAULT_CONFIG = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "desktop-model-providers.json"
MODEL_CATALOG_NAME = "chatgpt-desktop-multi-provider.json"
KEYRING_SERVICE = "chatgpt-desktop-multi-provider"

DEFAULT_PROVIDER_CONFIG: dict[str, Any] = {
    "version": 1,
    "default_provider": "openai",
    "providers": [
        {
            "id": "openai",
            "label": "ChatGPT / OpenAI",
            "description": "Uses your signed-in ChatGPT account",
        },
        {
            "id": "openrouter",
            "label": "OpenRouter",
            "description": "OpenRouter free-model router",
        },
    ],
    "model_providers": {
        "openrouter/free": "openrouter",
    },
}

CODEX_PROVIDER_CONFIGS = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "account": "openrouter",
    },
}


CENTRAL_HELPERS = r'''
async __codexLinuxReadProviderConfig() {
  const fallback = {
    version: 1,
    defaultProvider: "openai",
    providers: [
      { id: "openai", label: "ChatGPT / OpenAI", description: "Uses your signed-in ChatGPT account" },
      { id: "openrouter", label: "OpenRouter", description: "OpenRouter free-model router" },
    ],
    modelProviders: {
      "openrouter/free": "openrouter",
    },
  };
  try {
    const { codexHome } = await Ov("codex-home", { params: { hostId: "local" } });
    const separator = codexHome.includes("\\") && !codexHome.includes("/") ? "\\" : "/";
    const path = `${codexHome.replace(/[\\/]+$/u, "")}${separator}desktop-model-providers.json`;
    const { contents } = await Ov("read-file", { params: { hostId: "local", path } });
    const config = JSON.parse(contents);
    if (config?.version !== 1 || !Array.isArray(config.providers) || !config.providers.length ||
        typeof config.default_provider !== "string" || !config.model_providers ||
        typeof config.model_providers !== "object" || Array.isArray(config.model_providers)) {
      throw Error("Invalid provider routing configuration");
    }
    const ids = new Set(config.providers.map((provider) => provider?.id));
    if (!ids.has(config.default_provider) || Object.values(config.model_providers).some((id) => !ids.has(id))) {
      throw Error("Provider routing configuration references an unknown provider");
    }
    return { ...fallback, providers: config.providers, defaultProvider: config.default_provider,
      modelProviders: config.model_providers };
  } catch (error) {
    console.warn("Provider picker config unavailable; using fallback", error);
    return fallback;
  }
}
async __codexLinuxRouteParams(method, params) {
  if (method === "thread/list") {
    const value = params && typeof params === "object" ? params : {};
    return value.modelProviders == null ? { ...value, modelProviders: [] } : value;
  }
  if (method !== "thread/start" || !params || typeof params !== "object" || params.modelProvider != null) return params;
  const config = await this.__codexLinuxReadProviderConfig();
  let selection = "auto";
  try { selection = window.localStorage.getItem("codex.customProviderSelection.v1") || "auto"; } catch {}
  const provider = selection === "auto"
    ? (config.modelProviders[params.model] || config.defaultProvider)
    : config.providers.some((item) => item.id === selection) ? selection : config.defaultProvider;
  return { ...params, modelProvider: provider };
}
'''


PICKER_SCRIPT = r'''
;(() => {
  if (window.__codexDesktopModelProvidersLinuxV1) return;
  window.__codexDesktopModelProvidersLinuxV1 = true;
  const marker = "__codexDesktopModelProvidersLinuxV1Picker";
  const loadConfig = async () => {
    const fallback = {
      version: 1, defaultProvider: "openai",
      providers: [
        { id: "openai", label: "ChatGPT / OpenAI", description: "Uses your signed-in ChatGPT account" },
        { id: "openrouter", label: "OpenRouter", description: "OpenRouter free-model router" },
      ],
      modelProviders: {
        "openrouter/free": "openrouter",
      },
    };
    try {
      const { codexHome } = await Nd("codex-home", { params: { hostId: "local" } });
      const separator = codexHome.includes("\\") && !codexHome.includes("/") ? "\\" : "/";
      const path = `${codexHome.replace(/[\\/]+$/u, "")}${separator}desktop-model-providers.json`;
      const { contents } = await Nd("read-file", { params: { hostId: "local", path } });
      const value = JSON.parse(contents);
      if (value?.version !== 1 || !Array.isArray(value.providers) || !value.providers.length ||
          typeof value.default_provider !== "string" || !value.model_providers ||
          typeof value.model_providers !== "object" || Array.isArray(value.model_providers)) throw Error("Invalid provider config");
      const ids = new Set(value.providers.map((item) => item?.id));
      if (!ids.has(value.default_provider) || Object.values(value.model_providers).some((id) => !ids.has(id))) throw Error("Unknown provider in config");
      return { ...fallback, providers: value.providers, defaultProvider: value.default_provider, modelProviders: value.model_providers };
    } catch (error) {
      console.warn("Provider picker config unavailable; using fallback", error);
      return fallback;
    }
  };
  const visible = (element) => !!(element && element.getClientRects().length);
  const attach = async () => {
    const selected = [...document.querySelectorAll('[data-model-selected="true"]')].find(visible);
    const menu = selected?.closest('[role="menu"], [role="dialog"]');
    if (!menu || !visible(menu) || menu.querySelector(`[data-codex-provider-picker="${marker}"]`)) return;
    const config = await loadConfig();
    if (!menu.isConnected || menu.querySelector(`[data-codex-provider-picker="${marker}"]`)) return;
    const panel = document.createElement("section");
    panel.dataset.codexProviderPicker = marker;
    panel.setAttribute("aria-label", "Provider for new tasks");
    panel.style.cssText = "padding:10px 12px 8px;border-bottom:1px solid var(--border-subtle,rgba(128,128,128,.25));";
    const title = document.createElement("div");
    title.textContent = "Provider for new tasks";
    title.style.cssText = "font-size:12px;font-weight:600;opacity:.75;padding:2px 8px 6px;";
    panel.append(title);
    let current = "auto";
    try { current = localStorage.getItem("codex.customProviderSelection.v1") || "auto"; } catch {}
    const choices = [{ id: "auto", label: "Automatic", description: `Mapped provider; ${config.providers.find((item) => item.id === config.defaultProvider)?.label || config.defaultProvider} when unmapped` }, ...config.providers];
    for (const choice of choices) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${choice.id === current ? "✓  " : "   "}${choice.label}`;
      button.setAttribute("role", "menuitemradio");
      button.setAttribute("aria-checked", String(choice.id === current));
      button.style.cssText = "display:block;width:100%;text-align:left;border:0;border-radius:6px;background:transparent;color:inherit;padding:7px 9px;font:inherit;font-size:13px;cursor:pointer;";
      button.addEventListener("mouseenter", () => { button.style.background = "var(--background-modifier-hover,rgba(128,128,128,.14))"; });
      button.addEventListener("mouseleave", () => { button.style.background = "transparent"; });
      button.addEventListener("click", async (event) => {
        event.preventDefault(); event.stopPropagation();
        try { localStorage.setItem("codex.customProviderSelection.v1", choice.id); } catch {}
        try { await Nd("clear-prewarmed-threads-for-host", { hostId: "local" }); } catch {}
        panel.querySelectorAll("button").forEach((item) => {
          const checked = item === button;
          item.setAttribute("aria-checked", String(checked));
          item.textContent = `${checked ? "✓  " : "   "}${choices.find((entry) => entry.id === item.dataset.providerId)?.label || ""}`;
        });
        button.dataset.providerId = choice.id;
        button.textContent = `✓  ${choice.label}`;
      });
      button.dataset.providerId = choice.id;
      panel.append(button);
    }
    menu.prepend(panel);
  };
  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; attach().catch((error) => console.warn("Provider picker injection failed", error)); });
  };
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-model-selected", "aria-expanded", "data-state"] });
  schedule();
})();
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Installed Linux app bundle used as the source")
    parser.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL, help="Per-user patched app directory")
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER, help="Per-user launcher path")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Provider-routing JSON (created only when absent)")
    parser.add_argument("--check", action="store_true", help="Inspect version and patch anchors without installing")
    return parser.parse_args()


def package_version(source: Path) -> str:
    metadata_path = source / "resources" / "linux-package-metadata.json"
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        return str(data["version"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PatchError(f"Cannot read Linux package version from {metadata_path}: {exc}") from exc


def version_key(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+)+", value):
        raise PatchError(f"Unrecognized Linux app version: {value}")
    return tuple(int(part) for part in value.split("."))


def require_supported_version(value: str) -> None:
    current = version_key(value)
    minimum = version_key(MINIMUM_VERSION)
    width = max(len(current), len(minimum))
    if current + (0,) * (width - len(current)) < minimum + (0,) * (width - len(minimum)):
        raise PatchError(f"App version {value} is older than the minimum supported version {MINIMUM_VERSION}")


def _atomic_write_text(path: Path, contents: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _catalog_model(template: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(template)
    for key in ("model_messages", "base_instructions", "include_skills_usage_instructions",
                "include_plugin_usage_instructions", "include_apps_usage_instructions"):
        result.pop(key, None)
    result.update(model)
    result["base_instructions"] = (
        "You are Codex, an AI coding assistant. Follow the user's request, use available tools "
        "when appropriate, verify your work, and report results accurately. Never reveal secrets."
    )
    result["supported_reasoning_levels"] = [
        {"effort": "low", "description": "Default reasoning supported by the provider"}
    ]
    result["default_reasoning_level"] = "low"
    result["supports_search_tool"] = False
    result["experimental_supported_tools"] = []
    result["priority"] = 100
    return result


def provider_models(bundled_catalog: dict[str, Any]) -> list[dict[str, Any]]:
    models = bundled_catalog.get("models")
    if not isinstance(models, list) or not models:
        raise PatchError("Codex bundled model catalog has no models")
    template = next((item for item in models if item.get("slug") == "gpt-5.6-sol"), models[0])
    definitions = [
        {
            "slug": "openrouter/free",
            "display_name": "OpenRouter Free Router",
            "description": "OpenRouter free-model router; selects a backing model dynamically. 200K context; text and image input.",
            "context_window": 200_000,
            "max_context_window": 200_000,
            "input_modalities": ["text", "image"],
        },
    ]
    return [_catalog_model(template, item) for item in definitions]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _insert_root_setting(contents: str, key: str, value: str) -> str:
    lines = contents.splitlines(keepends=True)
    root_end = next((i for i, line in enumerate(lines) if re.match(r"\s*\[\[?[^]]+\]\]?\s*(?:#.*)?$", line)), len(lines))
    setting = f"{key} = {_toml_string(value)}\n"
    for index, line in enumerate(lines[:root_end]):
        if re.match(rf"\s*{re.escape(key)}\s*=", line):
            right_hand_side = line.split("=", 1)[1].strip()
            if right_hand_side.startswith(('"""', "'''")):
                raise PatchError(f"Cannot safely update multiline TOML setting {key}")
            comment = ""
            if "#" in line:
                comment = "  #" + line.split("#", 1)[1].rstrip("\r\n")
            lines[index] = setting.rstrip("\n") + comment + "\n"
            return "".join(lines)
    if root_end and lines[root_end - 1].strip():
        lines.insert(root_end, "\n")
        root_end += 1
    lines.insert(root_end, setting)
    return "".join(lines)


def configure_codex(source: Path, codex_home: Path) -> tuple[Path, Path]:
    if tomllib is None:
        raise PatchError("Python 3.11 or newer is required to safely configure Codex TOML")
    secret_tool = shutil.which("secret-tool")
    if secret_tool is None:
        raise PatchError("secret-tool is required to configure GNOME Keyring authentication")
    codex_binary = source / "resources" / "codex"
    if not codex_binary.is_file() or not os.access(codex_binary, os.X_OK):
        codex_binary = Path(shutil.which("codex") or "")
    if not codex_binary.is_file():
        raise PatchError("Cannot find the Codex CLI to read its bundled model catalog")

    config_path = codex_home / "config.toml"
    try:
        config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        config_data = tomllib.loads(config_text)
    except (OSError, ValueError) as exc:
        raise PatchError(f"Cannot safely read Codex configuration {config_path}: {exc}") from exc

    catalog_path = codex_home / "model-catalogs" / MODEL_CATALOG_NAME
    old_catalog_value = config_data.get("model_catalog_json")
    if "model_catalog_json" in config_data and not isinstance(old_catalog_value, str):
        raise PatchError("Codex model_catalog_json must be a string path")
    existing_models: list[dict[str, Any]] = []
    if isinstance(old_catalog_value, str):
        old_catalog_path = Path(old_catalog_value).expanduser()
        if not old_catalog_path.is_absolute():
            old_catalog_path = codex_home / old_catalog_path
        try:
            old_catalog = json.loads(old_catalog_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            old_catalog = {"models": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise PatchError(f"Cannot read existing model catalog {old_catalog_path}: {exc}") from exc
        if not isinstance(old_catalog, dict) or not isinstance(old_catalog.get("models"), list):
            raise PatchError(f"Existing model catalog has an invalid shape: {old_catalog_path}")
        if any(not isinstance(item, dict) or not isinstance(item.get("slug"), str) for item in old_catalog["models"]):
            raise PatchError(f"Existing model catalog contains an invalid model: {old_catalog_path}")
        existing_models = old_catalog["models"]

    try:
        result = subprocess.run(
            [str(codex_binary), "debug", "models", "--bundled"],
            check=True, capture_output=True, text=True,
        )
        catalog = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise PatchError(f"Cannot load bundled Codex model catalog: {exc}") from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise PatchError("Codex bundled model catalog has an invalid shape")

    merged: dict[str, dict[str, Any]] = {}
    for item in catalog["models"] + existing_models:
        if isinstance(item, dict) and isinstance(item.get("slug"), str):
            merged[item["slug"]] = item
    for item in provider_models(catalog):
        merged[item["slug"]] = item
    catalog["models"] = list(merged.values())

    provider_tables = config_data.get("model_providers", {})
    if not isinstance(provider_tables, dict):
        raise PatchError("Codex model_providers configuration must be a TOML table")
    additions = []
    for provider_id, details in CODEX_PROVIDER_CONFIGS.items():
        if provider_id in provider_tables:
            continue
        account = details["account"]
        additions.append(
            f"[model_providers.{provider_id}]\n"
            f"name = {_toml_string(details['name'])}\n"
            f"base_url = {_toml_string(details['base_url'])}\n"
            "wire_api = \"responses\"\n\n"
            f"[model_providers.{provider_id}.auth]\n"
            f"command = {_toml_string(secret_tool)}\n"
            f"args = [\"lookup\", \"service\", {_toml_string(KEYRING_SERVICE)}, \"provider\", {_toml_string(account)}]\n"
            "timeout_ms = 5000\n"
            "refresh_interval_ms = 0\n"
        )
    if additions:
        if config_text and not config_text.endswith("\n"):
            config_text += "\n"
        config_text += "\n" + "\n".join(additions)
    config_text = _insert_root_setting(config_text, "model_catalog_json", str(catalog_path))
    try:
        tomllib.loads(config_text)
    except ValueError as exc:
        raise PatchError(f"Generated Codex configuration is invalid TOML: {exc}") from exc

    atomic_write_json(catalog_path, catalog)
    _atomic_write_text(config_path, config_text)
    return config_path, catalog_path


def locate_bundles(extracted: Path) -> tuple[Path, Path]:
    assets = extracted / "webview" / "assets"
    if not assets.is_dir():
        raise PatchError("Extracted app has no webview/assets directory")
    central_candidates = []
    picker_candidates = []
    for path in assets.glob("*.js"):
        if path.name.endswith(".map.js"):
            continue
        data = path.read_text(encoding="utf-8")
        if "async sendRequest(" in data and "async prewarmThreadStart(" in data:
            central_candidates.append(path)
        if '[data-model-selected="true"]' in data and "modelOptionsDisabled" in data:
            picker_candidates.append(path)
    if len(central_candidates) != 1 or len(picker_candidates) != 1 or central_candidates == picker_candidates:
        raise PatchError(
            "Expected one Linux app-server bundle and one model-picker bundle; "
            f"found {len(central_candidates)} and {len(picker_candidates)}"
        )
    return central_candidates[0], picker_candidates[0]


def patch_bundles(central: Path, picker: Path) -> None:
    central_text = central.read_text(encoding="utf-8")
    picker_text = picker.read_text(encoding="utf-8")
    if CENTRAL_MARKER in central_text or PICKER_MARKER in picker_text:
        if CENTRAL_MARKER in central_text and PICKER_MARKER in picker_text:
            return
        raise PatchError("Only part of the Linux provider patch is present")

    request_anchor = "        async sendRequest(e, t, n) {"
    prewarm_anchor = "        async prewarmThreadStart(e, t) {"
    prewarm_positions = [i for i in range(len(central_text)) if central_text.startswith(prewarm_anchor, i)]
    if len(prewarm_positions) != 1:
        raise PatchError("Unsupported app-server bundle: prewarm method anchor is not unique")
    prewarm_index = prewarm_positions[0]
    class_index = central_text.rfind("class {", 0, prewarm_index)
    request_positions = [i for i in range(len(central_text)) if central_text.startswith(request_anchor, i)]
    request_positions = [i for i in request_positions if class_index < i < prewarm_index]
    if len(request_positions) != 1:
        raise PatchError("Unsupported app-server bundle: matching request method is not unique")
    request_index = request_positions[0]
    if "        async sendConfigReadRequest(e, t) {" not in central_text:
        raise PatchError("Unsupported app-server bundle: expected methods are missing")

    helper_methods = "        " + CENTRAL_MARKER + ";\n" + "\n".join(
        "        " + line if line else "" for line in CENTRAL_HELPERS.strip().splitlines()
    ) + "\n"
    central_text = central_text[:request_index] + helper_methods + central_text[request_index:]
    route_anchor = "          return e === `config/read`\n            ? this.sendConfigReadRequest(t, n)"
    request_index = central_text.index(request_anchor, class_index)
    prewarm_index = central_text.index(prewarm_anchor, request_index)
    request_section = central_text[request_index:prewarm_index]
    if request_section.count(route_anchor) != 1:
        raise PatchError("Unsupported app-server bundle: request dispatch anchor changed")
    request_section = request_section.replace(route_anchor, "          t = await this.__codexLinuxRouteParams(e, t);\n" + route_anchor, 1)
    central_text = central_text[:request_index] + request_section + central_text[prewarm_index:]
    prewarm_body = "        async prewarmThreadStart(e, t) {\n          if (this.dispatchMessage == null)"
    if central_text.count(prewarm_body) != 1:
        raise PatchError("Unsupported app-server bundle: prewarm request anchor changed")
    central_text = central_text.replace(
        prewarm_body,
        "        async prewarmThreadStart(e, t) {\n          e = await this.__codexLinuxRouteParams(`thread/start`, e);\n          if (this.dispatchMessage == null)",
        1,
    )

    if "data-model-selected" not in picker_text or picker_text.count("modelOptionsDisabled") < 1:
        raise PatchError("Unsupported model-picker bundle")
    picker_text += "\n/* " + PICKER_MARKER + " */\n" + PICKER_SCRIPT + "\n"
    central.write_text(central_text, encoding="utf-8")
    picker.write_text(picker_text, encoding="utf-8")


def launcher_text(app_dir: Path) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail
app_dir={shlex_quote(str(app_dir))}
platform_flags=()
if [[ -n "${{WAYLAND_DISPLAY:-}}" || "${{XDG_SESSION_TYPE:-}}" == wayland ]]; then
  platform_flags=(--ozone-platform=wayland)
  for arg in "$@"; do
    case "$arg" in --ozone-platform=*|--ozone-platform-hint=*) platform_flags=() ;; esac
  done
fi
exec "$app_dir/ChatGPT" "${{platform_flags[@]}}" "$@"
"""


def shlex_quote(value: str) -> str:
    return shlex.quote(value)


def run_installer(source: Path, install_dir: Path, launcher: Path, config: Path, check_only: bool) -> None:
    source = source.expanduser().resolve()
    install_dir = install_dir.expanduser().resolve()
    launcher = launcher.expanduser().resolve()
    config = config.expanduser().resolve()
    if sys.platform != "linux":
        raise PatchError("This installer supports Linux only")
    if not (source / "resources/app.asar").is_file():
        raise PatchError(f"Not a supported Linux app bundle: {source}")
    version = package_version(source)
    require_supported_version(version)
    if shutil.which("npx") is None or shutil.which("node") is None:
        raise PatchError("Node.js and npx are required")
    validate_provider_config(DEFAULT_PROVIDER_CONFIG)
    with tempfile.TemporaryDirectory(prefix="chatgpt-linux-provider-") as tmp:
        work = Path(tmp)
        extracted = work / "app"
        run(["npx", "--yes", ASAR_PACKAGE, "extract", str(source / "resources/app.asar"), str(extracted)], label="Extracting Linux app resources")
        central, picker = locate_bundles(extracted)
        run(["npx", "--yes", PRETTIER_PACKAGE, "--write", str(central), str(picker)], label="Formatting supported JavaScript bundles")
        patch_bundles(central, picker)
        run(["npx", "--yes", PRETTIER_PACKAGE, "--write", str(central), str(picker)], label="Validating patched JavaScript")
        patched_asar = work / "app.asar"
        run(["npx", "--yes", ASAR_PACKAGE, "pack", str(extracted), str(patched_asar)], label="Packing patched Linux app resources")
        with patched_asar.open("rb") as handle:
            header = handle.read(16)
        if len(header) != 16:
            raise PatchError("Packed ASAR is unexpectedly short")
        check_dir = work / "verify"
        run(["npx", "--yes", ASAR_PACKAGE, "extract", str(patched_asar), str(check_dir)], label="Verifying patched archive")
        verified_central, verified_picker = locate_bundles(check_dir)
        if CENTRAL_MARKER not in verified_central.read_text(encoding="utf-8") or PICKER_MARKER not in verified_picker.read_text(encoding="utf-8"):
            raise PatchError("Patched ASAR is missing required verification markers")
        if check_only:
            print(f"Compatible Linux app: {version}; bundles: {central.name}, {picker.name}")
            return

        source_config_state = ensure_provider_config(
            config, overwrite=False, default_config=DEFAULT_PROVIDER_CONFIG,
            merge_defaults=True,
        )
        print(f"Provider configuration {source_config_state}: {config}")
        codex_config_path, model_catalog_path = configure_codex(source, config.parent)
        print(f"Codex provider and model catalog configuration: {codex_config_path}")
        print(f"Codex model catalog: {model_catalog_path}")
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        stage = install_dir.parent / f".{install_dir.name}.staging-{os.getpid()}"
        old_copy = install_dir.parent / f"{install_dir.name}.previous"
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(source, stage, symlinks=True, copy_function=shutil.copy2)
        shutil.copy2(patched_asar, stage / "resources/app.asar")
        if old_copy.exists():
            shutil.rmtree(old_copy)
        if install_dir.exists():
            os.replace(install_dir, old_copy)
        try:
            os.replace(stage, install_dir)
        except Exception:
            if old_copy.exists() and not install_dir.exists():
                os.replace(old_copy, install_dir)
            raise

        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher_tmp = launcher.with_name(f".{launcher.name}.tmp-{os.getpid()}")
        launcher_tmp.write_text(launcher_text(install_dir), encoding="utf-8")
        launcher_tmp.chmod(0o755)
        os.replace(launcher_tmp, launcher)
        print(f"Installed {version} user copy: {install_dir}")
        print(f"Launch with: {launcher}")
        if old_copy.exists():
            print(f"Previous patched copy retained at: {old_copy}")


def main() -> int:
    args = parse_args()
    try:
        run_installer(args.source, args.install_dir, args.launcher, args.config, args.check)
    except (PatchError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
