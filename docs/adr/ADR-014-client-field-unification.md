# ADR-014: Unify inference-client identity under a single `client` field

**Status:** Accepted  
**Date:** 2026-06-08  
**Author:** @markgalpin

---

## Context

Three separate subsystems independently arrived at the concept of "which
inference client/tool does this agent use":

1. **`ax agents profiles` (PR #242)** — profile fragments are keyed by
   `client` (e.g. `claude`). `_gateway_runtime_to_client()` derives the
   client from `runtime_type`: both `claude_code_channel` and `sentinel_cli`
   map to `"claude"` because both run the Claude Code CLI and share its
   `.claude/settings.local.json` surface.

2. **`ax channel setup --client` (PR #232)** — channel setup accepts
   `--client` to select which MCP host (Claude Code, Cursor, Windsurf, …)
   to write config for. Currently `"claude"` is the only supported value,
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
| `claude_code_channel`    | `claude`       | Live channel listener (MCP bridge)        |
| `sentinel_cli`           | `claude`       | Spawned Claude CLI process per message    |
| `sentinel_inference_sdk` | `gemini_sdk`   | Spawned sentinel process, Gemini SDK      |
| `sentinel_inference_sdk` | `openai_sdk`   | Spawned sentinel process, OpenAI SDK      |
| `sentinel_inference_sdk` | `groq_sdk`     | Spawned sentinel process, Groq SDK        |
| `hermes_plugin`          | *(n/a)*        | Supervised `hermes gateway run` process   |
| `echo` / `exec`          | *(n/a)*        | Built-in / arbitrary exec                 |

## Decision

Adopt `client` as the canonical, unified field name across all three
subsystems:

### 1. Registry entry field

The registry entry for any agent that has an explicit inference client stores
it as `"client"`. The daemon reads it in this priority order for backwards
compatibility with pre-ADR-014 entries:

```python
entry.get("client")
  or entry.get("sentinel_sdk_runtime")   # ADR-012 name
  or entry.get("hermes_runtime")          # pre-ADR-012 name
  or entry.get("sdk_runtime")             # earliest name
```

`sentinel_cli` and `claude_code_channel` agents written by this version of
Gateway default to `client = "claude"` and store it in the registry entry,
making entries self-describing without requiring inference from `runtime_type`.
Other CLI clients (Cursor, Windsurf, …) are forward-compatible once their
sentinel support is implemented.

### 2. CLI flags

`ax gateway agents add` and `ax gateway agents update` gain `--client`:

```bash
ax gateway agents add slack-output \
  --type sentinel_inference_sdk \
  --workdir "$(pwd)" \
  --client gemini_sdk \
  --connector-ref composio-main \
  --allow-all-users

ax gateway agents update slack-output --client openai_sdk
```

For `claude_code_channel` and `sentinel_cli` the default is `"claude"` and
the flag is accepted but currently has no effect (future: Cursor, Windsurf).

### 3. Declarative manifest field

Agent manifests (PR #235) gain a `client` field:

```toml
name = "slack-output"
type = "sentinel_inference_sdk"
client = "gemini_sdk"
connector_ref = "composio-main"
allow_all_users = true
```

The manifest `client` maps to the `client` kwarg on `_register_managed_agent`
/ `_update_managed_agent` and is stored directly as `entry["client"]`.

### 4. Profiles — no change

`ax agents profiles` already uses `client` with the correct semantics.
`_gateway_runtime_to_client()` is extended to also read `entry.get("client")`
directly when present, so explicitly stored entries don't require inference.

### 5. Channel setup — no change

`ax channel setup --client` already uses the correct field name and concept.
No renaming needed; the `--client` flag there and the `--client` flag on
`agents add/update` refer to the same concept.

## Validation

For `sentinel_inference_sdk`, the set of valid `client` values is the same
as the former `_HERMES_SENTINEL_SDK_RUNTIMES` set:

```text
openai_sdk | gemini_sdk | groq_sdk | mistral_sdk | leapfrog_sdk | xai_sdk
```

The daemon rejects `sentinel_inference_sdk` agents with an unrecognised or
absent `client` at start time (same behaviour as today for
`sentinel_sdk_runtime`).

For `claude_code_channel` and `sentinel_cli`, the valid set today is
`{"claude"}`. Unknown values are warned but not rejected, to allow
forward-compat with future MCP hosts before the daemon is updated.

### Semantic boundary: tool clients vs. inference clients

`client = "claude"` for `sentinel_cli` and `claude_code_channel` refers to the
**Claude Code CLI tool** (the MCP host / coding-agent), not the Anthropic
inference API. If Anthropic's inference SDK is ever added as a backend for
`sentinel_inference_sdk`, its `client` value MUST be `anthropic_sdk`, not
`claude`, to preserve this distinction. In general, `sentinel_inference_sdk`
client values always use SDK names (`openai_sdk`, `gemini_sdk`,
`anthropic_sdk`, …); `sentinel_cli` and `claude_code_channel` client values
always use tool/host names (`claude`, and future MCP hosts). The two
namespaces do not overlap.

## Consequences

- **Breaking change in registry entry field name** for new entries: the field
  is now `client`, not `sentinel_sdk_runtime`. Existing entries continue to
  work via the fallback chain until they are re-saved by `agents update`.
- **No change to daemon logic** beyond the read-order fallback and the
  `_gateway_runtime_to_client` extension.
- **No change to profiles** beyond reading `entry["client"]` directly.
- **No change to channel setup.**
- `sentinel_sdk_runtime` is now a deprecated alias; remove from the fallback
  chain in a future major version once all known deployments have been
  migrated (track in issue #259 or a follow-on).
