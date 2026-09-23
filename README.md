# Better Codex App Custom Provider Support

An unofficial patch for the macOS ChatGPT/Codex desktop app that adds per-task model-provider selection without requiring you to sign out of your ChatGPT account.

The patch:

- Adds a provider section to the model menu.
- Sends the selected provider when a new task starts.
- Maps model IDs to providers automatically.
- Keeps tasks from all configured providers visible.
- Keeps the normal ChatGPT login active for OpenAI models.

The original installer supports macOS. A separate Linux installer targets the
official Linux package and installs a patched **per-user copy**; the minimum
Linux Codex build is `26.915.31945`.

> [!CAUTION]
> Changing the provider in a running conversation/thread does **not** work. The conversation/thread continues using the provider it started with.

<img width="500" src="https://github.com/user-attachments/assets/9b61e720-35e9-4021-9c6f-bd77e334e471" />
<br>

## Requirements

- macOS
- ChatGPT installed at `/Applications/ChatGPT.app`
- Python 3.9 or newer
- Node.js with `npx`

## Install

### macOS

<img width="600" src="https://github.com/user-attachments/assets/8800efd1-d490-4bc3-9959-47ddff5a6db8" />

<br>
<br>

<img width="600" src="https://github.com/user-attachments/assets/90461000-8b4b-4632-93cc-125a116b0830" />

<br>
<br>

1. Click on the `patch_chatgpt_providers.py` file in the repository.
2. Open the menu in the top-right.
3. Click on **Download**.
4. Run the downloaded script:

```bash
python3 patch_chatgpt_providers.py
```

### Linux (Omarchy / Arch and other Linux distributions)

Linux installation does not replace the package-managed application. It copies
the app to `~/.local/opt/chatgpt-provider-patched`, patches and verifies the
copy, and creates `~/.local/bin/chatgpt-providers` as a separate launcher.
The launcher keeps command-line arguments and selects Wayland when the session
uses Wayland, unless an Ozone platform flag was supplied explicitly.

Requirements: the official Linux Codex package at `/usr/lib/chatgpt`, Python
3.11+, Node.js, `npx`, and GNOME Keyring's `secret-tool`. The minimum supported build is `26.915.31945`; newer
numeric builds are accepted when their JavaScript bundles retain the required
patch anchors. The installer refuses older builds or changed JavaScript
structures before it replaces the user copy.

```bash
python3 patch_chatgpt_providers_linux.py --check
python3 patch_chatgpt_providers_linux.py
chatgpt-providers
```

The installer creates `~/.codex/desktop-model-providers.json` (or uses the
effective `CODEX_HOME`) when it is absent; if it already exists, it adds only
missing providers and model mappings. Existing defaults, labels and mappings
are preserved. It also adds missing OpenRouter and
AI-Flow provider sections to `config.toml`, then builds a complete model catalog
from the Codex binary shipped in the Linux package. Existing provider sections,
catalog models and credentials are preserved. The generated catalog is stored
at `~/.codex/model-catalogs/chatgpt-desktop-multi-provider.json` and selected
through `model_catalog_json`.

The model list contains:

| Provider | API model ID (catalog slug) | Display name | Context and input modalities |
| --- | --- | --- | --- |
| OpenRouter | `openrouter/free` | OpenRouter Free Router | 200K; text and image |
| AI-Flow | `glm-5.3` | `zai/glm-5.3 (AI-Flow)` | 1M; text only |
| AI-Flow | `glm-5.3-flash` | `zai/glm-5.3-flash (AI-Flow)` | 1M; text and image |
| AI-Flow | `nex` | `Nex (openai/nex, AI-Flow)` | 250K; text and image; slow prefill; local only |

AI-Flow's API accepts the unprefixed slugs shown above. The requested `zai/`
and `openai/` forms receive HTTP 403 from that endpoint. AI-Flow context sizes
and the Nex notes are user-provided metadata; endpoint smoke tests verified
text responses and image input, not those metadata claims. OpenRouter's free
router may select a different backing model between requests.

The custom providers read their bearer tokens from GNOME Keyring using
`secret-tool`. Create new API keys before setup: the previous values were
exposed in a local diagnostic output. Store each new key without adding it to
shell history:

```bash
read -r -s -p "New OpenRouter API key: " api_key; printf '\n'
printf '%s' "$api_key" | secret-tool store --label="Codex OpenRouter" service chatgpt-desktop-multi-provider provider openrouter
unset api_key

read -r -s -p "New AI-Flow API key: " api_key; printf '\n'
printf '%s' "$api_key" | secret-tool store --label="Codex AI-Flow" service chatgpt-desktop-multi-provider provider ai-flow
unset api_key
```

After verifying the Keyring entries, remove the old `OPENROUTER_API_KEY` and
`LITELLM_API_KEY` exports from `~/.bashrc`; that file should not contain the
rotated keys. Do not put API keys in `config.toml`, the routing JSON, or the
model catalog.

The Codex configuration uses `wire_api = "responses"` and a `secret-tool
lookup` auth command for each provider. Inspect the effective catalog with
`/usr/lib/chatgpt/resources/codex debug models` and restart the patched app
after installation or catalog changes.

After an application package update, run the Linux installer again. It checks
that the package meets the minimum version and that the expected patch anchors
still match before replacing the user copy. To return to the package-managed
app, launch `chatgpt` as usual; the system package and desktop entry are left
unchanged. The previous user copy is retained at
`~/.local/opt/chatgpt-provider-patched.previous` when replaced.

The macOS installer closes processes belonging to the target app, creates a complete backup, patches `app.asar`, updates Electron's ASAR integrity metadata, and applies an ad-hoc signature.

Run `python3 patch_chatgpt_providers.py --help` to see alternate app, config, and backup paths.

## Configure a custom provider

Custom Codex providers belong in `~/.codex/config.toml`. Provider IDs such as `openrouter` are referenced by the patch configuration later.

Example OpenRouter provider:

```toml
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"

[model_providers.openrouter.auth]
command = "/usr/bin/secret-tool"
args = ["lookup", "service", "chatgpt-desktop-multi-provider", "provider", "openrouter"]
timeout_ms = 5000
refresh_interval_ms = 0
```

AI-Flow uses the same auth fields with `base_url = "https://litellm.ai-flow.no/v1"` and the final `provider` lookup value set to `ai-flow`. On Linux the installer adds these sections automatically when they are missing. On macOS, use the platform's secure credential store and its corresponding auth command instead.

Codex also supports environment-variable authentication with `env_key`. Do not combine `env_key` with a `[model_providers.<id>.auth]` section. For all authentication methods and provider options, see the [Codex custom model provider documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers).

Do not set a global `model_provider` if OpenAI and custom providers should coexist in the desktop app. The patch selects the provider when each new task starts.

## Add custom models to Codex

Codex loads custom model metadata from the file configured by `model_catalog_json`.

1. Export the bundled catalog as a starting point:

   ```bash
   mkdir -p ~/.codex/model-catalogs
   codex debug models --bundled > ~/.codex/model-catalogs/custom.json
   ```

2. Add model objects to the top-level `models` array. Copy an existing entry with similar capabilities, then update its model ID, display name, context window, modalities, reasoning levels, and tool support.

   The model ID is the `slug` value. It must match the model ID expected by the provider, for example:

   ```json
   {
     "slug": "moonshotai/kimi-k3",
     "display_name": "Kimi K3 (OpenRouter)",
     "description": "MoonshotAI Kimi K3 through OpenRouter."
   }
   ```

   Keep the remaining required fields from the copied entry and adjust them to the model's real capabilities. Do not advertise unsupported tools, modalities, or context-window sizes.

3. Point Codex at the catalog in `~/.codex/config.toml`:

   ```toml
   model_catalog_json = "/Users/your-name/.codex/model-catalogs/custom.json"
   ```

4. Restart the app after changing `model_catalog_json` or the model catalog.

Use `codex debug models` to inspect the effective catalog Codex sees.

## Configure the patched provider menu

The Linux installer creates:

```text
~/.codex/desktop-model-providers.json
```

Example:

```json
{
  "version": 1,
  "default_provider": "openai",
  "providers": [
    {
      "id": "openai",
      "label": "ChatGPT / OpenAI",
      "description": "Uses your signed-in ChatGPT account"
    },
    {
      "id": "openrouter",
      "label": "OpenRouter",
      "description": "OpenRouter free-model router"
    },
    {
      "id": "ai_flow",
      "label": "AI-Flow",
      "description": "AI-Flow hosted Codex models"
    }
  ],
  "model_providers": {
    "openrouter/free": "openrouter",
    "glm-5.3": "ai_flow",
    "glm-5.3-flash": "ai_flow",
    "nex": "ai_flow"
  }
}
```

- `providers` defines the providers displayed in the app menu.
- `model_providers` maps exact model slugs to provider IDs for Automatic mode.
- `default_provider` handles models without an explicit mapping.
- Every custom provider ID must match a `[model_providers.<id>]` section in `config.toml`.
- API keys do not belong in this JSON file.

The app reloads this file when the provider menu opens and before a new task starts. Repatching is not required after editing it.

## Updates and recovery

ChatGPT updates replace the patch. Run the installer again after an update.

The installer is not tied to a fixed app version or archive hash. It patches compatible source structures and stops before modifying the installed app if an update changes the relevant code.

Backups are stored by default in:

```text
~/Applications/ChatGPT Patch Backups/
```

## Disclaimer

Use this script entirely at your own risk. It modifies the installed ChatGPT application in an unofficial and unsupported way.

The author and contributors provide no warranty and accept no responsibility or liability for any problems, damage, or loss caused directly or indirectly by using this script. This includes, but is not limited to, lost or corrupted chat history or other data, an unusable or "bricked" application, account warnings or restrictions, account suspension or banning, security or privacy issues, and any other direct or consequential damage.

Create and verify your own backups before running the script.

---

_This is an unofficial modification and is not affiliated with or supported by OpenAI._
