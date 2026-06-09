# Hermes Agent Sentinel — Vendored

This package ships the Hermes Agent CLI sentinel that the gateway's `hermes` template launches.

## Origin

Vendored from the live ax-agents host (`/home/ax-agent/agents/` on the EC2 production host) on 2026-04-25 per @madtank's directive — "we own both repositories... copy over the files on my local machine to this repository."

## What's here

| File | Source (live host) | Lines |
| --- | --- | --- |
| `sentinel.py` | `claude_agent_v2.py` | 1641 |
| `runtimes/__init__.py` | `runtimes/__init__.py` | 142 |
| `runtimes/hermes_sdk.py` | `runtimes/hermes_sdk.py` | 474 |
| `runtimes/claude_cli.py` | `runtimes/claude_cli.py` | 178 |
| `runtimes/codex_cli.py` | `runtimes/codex_cli.py` | 155 |
| `runtimes/openai_sdk.py` | `runtimes/openai_sdk.py` | 502 |
| `tools/__init__.py` | `agents/tools/__init__.py` | 294 |

Each file carries a `# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md` line at the top for attribution.

## Runtime support

The vendored `sentinel.py` supports the following `--runtime` values:

| `--runtime` | Gateway runtime type | Notes |
| --- | --- | --- |
| `hermes_sdk` | `sentinel_hermes_sdk` | In-process Hermes AIAgent loop; Bedrock, OpenRouter, Anthropic |
| `openai_sdk` | `sentinel_inference_sdk` | Direct OpenAI API calls |
| `groq_sdk` | `sentinel_inference_sdk` | Direct Groq API calls |
| `gemini_sdk` | `sentinel_inference_sdk` | Direct Gemini API calls |
| `mistral_sdk` | `sentinel_inference_sdk` | Direct Mistral API calls |
| `leapfrog_sdk` | `sentinel_inference_sdk` | Direct Leapfrog API calls |
| `xai_sdk` | `sentinel_inference_sdk` | Direct xAI API calls |
| `claude_cli` | `sentinel_cli` | Claude Code subprocess (`claude -p`) |

`codex_cli` was removed in 0.7.0 (ADR-012). Gateway dispatches `sentinel_hermes_sdk` and `sentinel_inference_sdk` through `_start_sentinel_inference_sdk_process`; the resolved `--runtime` value is passed as a parameter.

## Wiring

`ax_cli/gateway.py` `_sentinel_inference_sdk_script(entry)` should resolve to:

- `Path(__file__).parent.parent / "runtimes" / "hermes" / "sentinel.py"` (the bundled path), OR
- An operator override at `/home/ax-agent/agents/claude_agent_v2.py` if it exists (preserves the dev-fleet workflow on the EC2 production host).

The override-then-bundle order means the existing dev fleet keeps using the live host copy while fresh `pip install ax-cli` users get the bundled one transparently.

### `tools/` shim — important

The `_secure_hermes_tools` function in `runtimes/hermes_sdk.py` does TWO imports that resolve to **different `tools` packages on the live host**:

```python
from tools.registry import registry          # → public hermes-agent's tools/registry.py
from tools import _check_read_path, ...      # → vendored tools/__init__.py (this dir)
```

On the EC2 production host, this works because PYTHONPATH puts `/home/ax-agent/agents` first (loads `tools/__init__.py` from there) and the public hermes-agent clone second (provides `tools.registry` via Python's namespace fall-through).

**For a `pip install ax-cli` user** wanting to launch a hermes agent, the wiring needs to:

1. Prepend `Path(__file__).parent` (i.e. `ax_cli/runtimes/hermes/`) to `sys.path` BEFORE the public hermes-agent clone, so `import tools` resolves to the vendored `tools/__init__.py` shim.
2. Ensure the public hermes-agent clone is also on `sys.path` (operators set this via `HERMES_REPO_PATH` or default `~/hermes-agent`) so `tools.registry` resolves correctly.

`_sentinel_inference_sdk_script` (the launcher) is the right place to set this up, since it constructs the subprocess env. The vendored `sentinel.py` does not need to be modified — the path setup happens at launch time.

### Why the shim isn't a separate import name

Renaming to e.g. `from ax_cli.runtimes.hermes.security import _check_read_path` would be cleaner, BUT it would diverge the vendored `runtimes/hermes_sdk.py` from the live host's copy. That breaks the "re-vendor as a clean copy" property. Keeping `from tools import ...` means the vendored runtime is byte-identical to live (modulo the attribution header), and the import resolution is a deployment concern, not a code change.

## Lint

Vendored files are excluded from `ruff` checks via `extend-exclude` in `pyproject.toml`. They follow the upstream ax-agents style (which differs from ax-cli's `select = ["E","F","W","I"]` profile). Updating the vendored files means re-vendoring from the live host — see "Re-vendoring" below.

## License

Both the `ax-agents` source and `ax-cli` destination are owned by aX Platform / @madtank. ax-cli is MIT (see `/LICENSE` at repo root). These vendored files inherit the ax-cli MIT license per @madtank's verbal license greenlight on 2026-04-25.

## Re-vendoring

When the live host's `claude_agent_v2.py` or `runtimes/` evolve, re-sync into this directory by running (on the EC2 host):

```bash
HEADER="# Vendored from ax-agents on $(date +%Y-%m-%d) — see ax_cli/runtimes/hermes/README.md"
SRC=/home/ax-agent/agents
DEST=/path/to/ax-cli/ax_cli/runtimes/hermes
{ echo "$HEADER"; cat "$SRC/claude_agent_v2.py"; } > "$DEST/sentinel.py"
for r in __init__ hermes_sdk claude_cli codex_cli openai_sdk; do
  { echo "$HEADER"; cat "$SRC/runtimes/$r.py"; } > "$DEST/runtimes/$r.py"
done
{ echo "$HEADER"; cat "$SRC/tools/__init__.py"; } > "$DEST/tools/__init__.py"
```

Then commit + PR. Update the line counts table in this README to reflect the new state.

## End-user setup (`sentinel_hermes_sdk` only)

The vendored sentinel is bundled with ax-cli, but the `hermes_sdk` backend's runtime dependencies live in the `NousResearch/hermes-agent` repo and must be installed separately. `sentinel_inference_sdk` backends (`openai_sdk`, `groq_sdk`, etc.) do not need this step.

```bash
git clone https://github.com/NousResearch/hermes-agent ~/hermes-agent
cd ~/hermes-agent
python3 -m venv .venv
.venv/bin/pip install -e .
```

The gateway auto-detects `~/hermes-agent` (or `$HERMES_REPO_PATH`) and uses its `.venv/bin/python3` when launching the sentinel. See [SETUP-HERMES.md](../../../docs/SETUP-HERMES.md) for the full operator walkthrough.
