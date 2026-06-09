# Declarative Agent Manifests

> Implements [GH #91](https://github.com/FulcrumDefense/fulcrum-gateway/issues/91).

`ax gateway agents apply <manifest.toml>` lets operators describe an agent's
intended configuration in a committed TOML file instead of accumulating it
through a runbook of `ax gateway agents add` / `update` commands. Re-applying
an unchanged manifest is a no-op; re-applying a changed one updates only the
fields that differ.

`ax gateway agents export <name>` writes the current registry state back to a
manifest file so operators can capture in-place configuration as a
source-of-truth file.

## Why declarative manifests

Three problems the imperative CLI doesn't solve well:

1. **Devcontainer / new-machine setup** — currently a runbook of imperative
   commands. With a manifest, one line:
   ```
   ax gateway agents apply /workspace/.ax/nova.agent.toml
   ```

2. **Source of truth** — `agents show` displays *current* state, not *intent*.
   A committed manifest is the intent; `apply --diff` shows the drift.

3. **Operator handoffs** — a committed manifest is explicit handoff
   documentation that doesn't depend on the previous operator remembering
   every flag they used.

## Manifest schema (TOML)

Example:

```toml
name = "nova"
template = "hermes"
space = "andrewprograde-workspace"
workdir = "/workspace"
allow_all_users = true
description = "Hermes agent for the nova workspace"
model = "codex:gpt-5.5"
timeout_seconds = 300

system_prompt = '''
You are nova. Answer questions about the workspace and run repo
investigations on request. Default to terse answers.
'''
```

### Field reference

| Manifest field | Maps to CLI flag | Notes |
|---|---|---|
| `name` | `--name` / argument | **Required.** The agent identity. |
| `template` | `--template` | Conditional. One of the registered templates (`hermes`, `claude_code_channel`, etc.). |
| `type` | `--type` | Advanced. Runtime backend (`hermes_plugin`, `exec`, `inbox`, ...). Mutually exclusive with `template` in practice. |
| `space` | `--space` | Target space slug, name, or UUID. |
| `workdir` | `--workdir` | Conditional — required for templates that bind to a workspace. |
| `description` | `--description` | Platform agent description. |
| `model` | `--model` | LLM model identifier. |
| `system_prompt` | `--system-prompt` | Inline operator-supplied system instructions. Mutually exclusive with `system_prompt_file`. |
| `system_prompt_file` | `--system-prompt-file` | Path to a file whose contents become the system prompt. Mutually exclusive with `system_prompt`. |
| `timeout_seconds` | `--timeout` | Max seconds the runtime may spend on one message. |
| `allow_all_users` | `--allow-all-users` | Hermes plugin only. Boolean. Default `false`. |
| `allowed_users` | `--allowed-users` | Hermes plugin only. Comma-separated handles. |
| `exec_command` | `--exec` | Advanced override for exec-based templates. |
| `model` | `--model` | Inference model name. For `sentinel_inference_sdk`: API model (e.g. `gemini-2.0-flash`). For `ollama` template: local model name (e.g. `gemma4:latest`). Previously `ollama_model` for Ollama — removed as a breaking change. |
| `connector_ref` | `--connector-ref` | Outbound connector name (required for `langgraph_composio`). |
| `audience` | `--audience` | Register-time only. Default `"both"`. |

### Validation rules

`parse_manifest` enforces:

- `name` is present and non-empty
- `system_prompt` and `system_prompt_file` are mutually exclusive
- Unknown keys are rejected (typo guard — manifests are operator-authored, and
  a silently-ignored typo is a worse failure mode than a parse error)
- `timeout_seconds` is a positive integer
- `allow_all_users` is a boolean

## Commands

### `apply`

```
ax gateway agents apply <manifest.toml>                  # idempotent apply
ax gateway agents apply <manifest.toml> --diff           # show changes, no apply
ax gateway agents apply <manifest.toml> --plan           # alias for --diff
ax gateway agents apply <manifest.toml> --auto-confirm   # skip interactive prompt
ax gateway agents apply <manifest.toml> --json           # JSON-shape result
```

**Apply semantics:**

1. Read and validate the manifest
2. Look up the agent by `name`; create if missing, update if present
3. Compute and display the diff
4. Prompt for confirmation in interactive mode (TTY); skip with `--auto-confirm`
5. Call `_register_managed_agent()` (create) or `_update_managed_agent()` (update)
6. Print new state on success

**Idempotency:** Fields **absent from the manifest** map to the `_UNSET` sentinel
on the update helper, so the existing value is preserved untouched. Fields
**present in the manifest** are applied. An explicit empty string clears the
field on update — same as passing an empty `--system-prompt`.

**Non-interactive contexts:** `apply` refuses to mutate non-interactively
without `--auto-confirm`. CI, devcontainer init, and scripted callers must
pass `--auto-confirm` (or `-y`).

### `export`

```
ax gateway agents export <name>                  # to stdout
ax gateway agents export <name> -o <file.toml>   # to file
```

`export` projects the live registry entry into manifest shape, dropping
`None` and empty-string values so re-applying the exported manifest is a
no-op. Pair with `apply --diff` to verify the round trip:

```
ax gateway agents export nova -o nova.exported.toml
ax gateway agents apply nova.exported.toml --diff   # should show "(no changes)"
```

## Diff output

`--diff` produces a stable, line-oriented format:

```
Planned: UPDATE @nova
  ~ workdir: /old → /workspace
  + description: Hermes agent for the nova workspace
  ~ timeout_seconds: 120 → 300
```

Symbols:

- `~ field: <before> → <after>` — value changes
- `+ field: <after>` — field is being set (create or add)
- `(no changes)` — manifest matches current state

Multi-line values (notably `system_prompt`) are summarized to the first line
so the diff stays readable in a terminal. Single-line values longer than 120
chars are truncated with `…`.

## Devcontainer workflow

A common use case: a committed `.ax/<name>.agent.toml` in a repo makes the
gateway bootstrap one-liner:

```dockerfile
# In devcontainer post-create script
ax gateway start --background
ax gateway agents apply /workspace/.ax/nova.agent.toml --auto-confirm
```

Operators can then `ax gateway agents export nova -o /workspace/.ax/nova.agent.toml`
when they tweak settings, and commit the diff so the next devcontainer build
inherits the change.

## Out of scope for v1

Per the issue's open questions, deliberately deferred:

- **`desired_state` in manifest** — currently set via `ax gateway agents start` /
  `stop` / `--desired-state`. Could be added in v2; for v1, manifests describe
  configuration, runtime state is operator-managed.
- **YAML / JSON formats** — only TOML for v1. The parser is a thin slice; a
  second format would slot in without touching the schema or apply semantics.
- **Layered/overlay manifests** — a single flat file for v1.
- **REST/HTTP `apply` endpoint** — CLI only for v1.
- **Daemon-side reconciler** — `apply` is operator-driven; there's no
  background process watching the manifest file.

## Implementation

Module: `ax_cli/agent_manifests.py` (parse + validate + diff + serialization)
Commands: `ax_cli/commands/gateway.py::apply_manifest` and `export_manifest`
Tests: `tests/test_agent_manifests.py` (32 cases covering parsing,
validation, diff, kwargs construction, and TOML round-trip)

The manifest parser uses `tomllib` from the Python 3.11+ stdlib, so no new
runtime dependency.
