#!/usr/bin/env python3
"""Install the provider picker into a per-user copy of the Linux Codex app.

The system package is read as the source and is never modified. JavaScript
patches are version-sensitive and the install copy is swapped only after ASAR
packing and marker checks succeed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from patch_chatgpt_providers import (
    DEFAULT_PROVIDER_CONFIG,
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


CENTRAL_HELPERS = r'''
async __codexLinuxReadProviderConfig() {
  const fallback = {
    version: 1,
    defaultProvider: "openai",
    providers: [
      { id: "openai", label: "ChatGPT / OpenAI", description: "Uses your signed-in ChatGPT account" },
      { id: "openrouter", label: "OpenRouter", description: "Uses [model_providers.openrouter] from config.toml" },
    ],
    modelProviders: {
      "moonshotai/kimi-k3": "openrouter",
      "x-ai/grok-4.5": "openrouter",
      "anthropic/claude-fable-5": "openrouter",
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
        { id: "openrouter", label: "OpenRouter", description: "Uses [model_providers.openrouter] from config.toml" },
      ],
      modelProviders: {
        "moonshotai/kimi-k3": "openrouter", "x-ai/grok-4.5": "openrouter",
        "anthropic/claude-fable-5": "openrouter",
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

        source_config_state = ensure_provider_config(config, overwrite=False)
        print(f"Provider configuration {source_config_state}: {config}")
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
