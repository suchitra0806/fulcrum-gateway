# Scenario: Set Up a Second Agent in the Same Workspace

## Goal

Register a second agent that shares a workspace directory with an existing
agent, while maintaining distinct identities.

## Prerequisites

- Gateway running
- First agent already registered and working in the target workspace directory
- Understanding that workspace identity is directory-scoped, not repo-scoped

## Steps

### 1. Check existing agent in the workspace

```bash
cd /path/to/shared-workspace
ax gateway agents show first-agent
```

Note the workdir and space binding.

### 2. Register the second agent

```bash
ax gateway agents add second-agent \
  --template pass_through \
  --workdir /path/to/shared-workspace
```

Gateway creates a separate registry entry with its own managed credential,
session tokens, and space binding — even though the workdir is the same.

### 3. Start the second agent

```bash
ax gateway agents start second-agent
```

### 4. Verify identity isolation

```bash
ax gateway agents show first-agent --json | grep agent_id
ax gateway agents show second-agent --json | grep agent_id
```

**Expected:** Different `agent_id` values. Each agent has its own identity on
the platform.

### 5. Test independent messaging

```bash
ax send "hello from test" --to first-agent --skip-ax
ax send "hello from test" --to second-agent --skip-ax

ax gateway agents inbox first-agent
ax gateway agents inbox second-agent
```

Messages should route to the correct agent based on the `--to` target, not the
shared workspace directory.

## Verify

- Both agents have distinct `agent_id` values
- Messages route to the correct agent
- Each agent has its own entry in `registry.json`
- Each agent has its own credential in `~/.ax/gateway/agents/<name>/token`
- The shared `.ax/config.toml` does not contain credentials for either agent
  (credentials are brokered by Gateway, not written to workspace config)

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| Both agents share the same identity | `.ax/config.toml` in the workspace specifies a single `agent_name`/`agent_id` | Gateway-managed agents use registry identity, not workspace config. Verify with `agents show` |
| Messages go to wrong agent | Agent name typo in `--to` flag | Double-check agent names with `ax gateway agents list` |
| Second agent can't start | Port or process conflict | Different agents can run simultaneously — check `gateway.log` for specific error |
| Credential confusion | Workspace config has a direct-token profile that shadows Gateway identity | Remove `token` from `.ax/config.toml` — let Gateway broker credentials per ADR-005 |

## Learning goal

Understanding workspace identity scoping. Gateway tracks agents by name in
`registry.json`, not by workspace directory. Two agents can share a workdir
because their credentials, sessions, and inbox queues are stored in Gateway's
state directory (`~/.ax/gateway/agents/<name>/`), not in the workspace. The
`.ax/config.toml` in the workspace is for CLI config resolution, not for
credential storage. See [ADR-005](../adr/ADR-005-credentials-never-in-workspace.md).
