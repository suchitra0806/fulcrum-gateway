# ADR-014: Unify inference-client identity under a single `client` field; unify model under `model`

**Status:** Accepted  
**Date:** 2026-06-08  
**Author:** @markgalpin

---

## Context

Three separate subsystems independently arrived at the concept of "which
inference client/tool does this agent use":

1. **`ax agents profiles` (PR #242)** — profile fragments are keyed by
   `client` (e.g. `claude_cli`). `_gateway_runtime_to_client()` derives the
   client from `runtime_type`: both `claude_code_channel` and `sentinel_cli`
   map to `"claude_cli"` because both run the Claude Code CLI and share its
   `.claude/settings.local.json` surface.

2. **`ax channel setup --client` (PR #232)** — channel setup accepts
   `--client` to select which MCP host (Claude Code, Cursor, Windsurf, …)
   to write config for. Currently `"claude_cli"` is the only supported value,
   but the abstraction exists for future MCP hosts.

3. **`sentinel_inference_sdk` / `sentinel_sdk_runtime` (ADR-012 / PR #236)**
   — agents using the vendor SDK runtime require an explicit sub-runtime
   selector (`openai_sdk`, `gemini_sdk`, `groq_sdk`, `mistral_sdk`,
   `leapfrog_sdk`, `xai_sdk`). This was stored as `sentinel_sdk_runtime`
   in the registry entry and had no CLI flag — operators had to use the
   error-message hint `--set sentinel_sdk_runtime=...` which doesn't exist.

All three are answering the same question: **"which underlying
inference client/tool handles this agent's reasoning?"**

The `runtime_type` field answers a different question: **"how does Gateway
supervise the agent process?"** These two axes are orthogonal:

| `runtime_type`           | `client`       | Supervision model                         |
|--------------------------|----------------|-------------------------------------------|
| `claude_code_channel`    | `claude_cli`   | Live channel listener (MCP bridge)        |
| `sentinel_cli`           | `claude_cli`   | Spawned Claude CLI process per message    |
| `sentinel_inference_sdk` | `gemini_sdk`   | Spawned sentinel process, Gemini SDK      |
| `sentinel_inference_sdk` | `openai_sdk`   | Spawned sentinel process, OpenAI SDK      |
| `sentinel_inference_sdk` | `groq_sdk`     | Spawned sentinel process, Groq SDK        |
| `hermes_plugin`          | *(n/a)*        | Supervised `hermes gateway run` process   |
| `echo` / `exec`          | *(n/a)*        | Built-in / arbitrary exec                 |

A second unification opportunity also resolved in this ADR:

**`ollama_model` → `model`** — The `ollama` template previously stored its
local model name in a separate registry field `ollama_model`, while
`sentinel_inference_sdk` used `model`. These are the same concept (the model
this agent reasons with) and are now unified under `model`. The `ollama_model`
field is removed as a breaking change.

## Decision

### 1. Registry entry field: `client`

The registry entry for any agent that has an explicit inference client stores
it as `"client"`. No fallback chain — breaking change. Operators must re-save
existing entries via `agents update --client <value>`.

`sentinel_cli` and `claude_code_channel` agents default to
`client = "claude_cli"` and store it in the registry entry, making entries
self-describing without requiring inference from `runtime_type`.

### 2. `claude_cli` — tool name, not model name

The client value for Claude Code CLI agents is `"claude_cli"`, not `"claude"`.
This was renamed from `"claude"` to make the distinction explicit: `claude_cli`
refers to the **Claude Code CLI tool** (the MCP host / coding-agent), not the
Anthropic inference API or model. Profiles and channel setup PRs (#242, #232)
are updated in the same release window.

### 3. Registry entry field: `model`

The `model` field is the single unified field for "which model does this agent
use." It replaces the former `ollama_model` field for Ollama template agents.

| Template / runtime         | `model` example         |
|----------------------------|-------------------------|
| `ollama`                   | `gemma4:latest`         |
| `sentinel_inference_sdk`   | `gemini-2.0-flash`      |
| `hermes_plugin`            | resolved from config    |

`ollama_model` is removed from the registry schema as a breaking change.
Agents with `ollama_model` in existing registry entries must be updated via
`agents update --model <value>`.

### 4. CLI flags

`ax gateway agents add` and `ax gateway agents update` gain `--client` and
use `--model` (unified, replaces `--ollama-model`):

```bash
ax gateway agents add slack-output \
  --type sentinel_inference_sdk \
  --client gemini_sdk \
  --model gemini-2.0-flash \
  --connector-ref composio-main \
  --allow-all-users

ax gateway agents add gemma4 \
  --template ollama \
  --model gemma4:latest

ax gateway agents update slack-output --client openai_sdk
```

### 5. Declarative manifest fields

Agent manifests (PR #235) gain `client` and use unified `model`:

```toml
name = "slack-output"
type = "sentinel_inference_sdk"
client = "gemini_sdk"
model = "gemini-2.0-flash"
connector_ref = "composio-main"
allow_all_users = true
```

### 6. Profiles

`ax agents profiles` uses `client` with the correct semantics.
`_gateway_runtime_to_client()` maps both `claude_code_channel` and
`sentinel_cli` to `"claude_cli"` (renamed from `"claude"`). Profile fragment
directories are renamed: `agent_profiles/claude/` → `agent_profiles/claude_cli/`.

### 7. Channel setup

`ax channel setup --client` already uses `--client`. The default value
updated from `"claude"` to `"claude_cli"`.

## Validation

For `sentinel_inference_sdk`, the valid `client` values:

```text
openai_sdk | gemini_sdk | groq_sdk | mistral_sdk | leapfrog_sdk | xai_sdk
```

The daemon rejects `sentinel_inference_sdk` agents with an unrecognised or
absent `client` at start time. No default — absent `client` is a setup error.

For `claude_code_channel` and `sentinel_cli`, the valid set today is
`{"claude_cli"}`. Unknown values are warned but not rejected, to allow
forward-compat with future MCP hosts before the daemon is updated.

### Semantic boundary: tool clients vs. inference clients

`client = "claude_cli"` for `sentinel_cli` and `claude_code_channel` refers
to the **Claude Code CLI tool** (the MCP host / coding-agent), not the
Anthropic inference API. If Anthropic's inference SDK is ever added as a
backend for `sentinel_inference_sdk`, its `client` value MUST be
`anthropic_sdk`, not `claude_cli`, to preserve this distinction.

In general, `sentinel_inference_sdk` client values always use SDK names
(`openai_sdk`, `gemini_sdk`, `anthropic_sdk`, …); `sentinel_cli` and
`claude_code_channel` client values always use tool/host names (`claude_cli`,
and future MCP hosts). The two namespaces do not overlap.

## Breaking changes summary

| Change | Old value | New value |
| --- | --- | --- |
| Client field name | `sentinel_sdk_runtime` / `hermes_runtime` | `client` |
| Claude Code client value | `claude` | `claude_cli` |
| Ollama model field | `ollama_model` | `model` |
| CLI flag for Ollama model | `--ollama-model` | `--model` |
| Profile fragment directory | `agent_profiles/claude/` | `agent_profiles/claude_cli/` |

All changes are in effect as of PR #236. No fallback chain is implemented —
existing registry entries with old field names must be updated.

## Consequences

- **Breaking change** for all three renames. Operators with existing agents
  must run `agents update` to migrate registry entries.
- The manifest schema is clean: `client` + `model` with no legacy aliases.
- Profiles and channel setup converge on the same `claude_cli` value,
  eliminating the ambiguity between "Claude the AI" and "claude-code the CLI".
