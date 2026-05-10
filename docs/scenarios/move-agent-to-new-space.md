# Scenario: Move an Agent to a New Space

## Goal

Move a managed agent from one space to another without losing its registration
or credentials.

## Prerequisites

- Gateway running (`ax gateway status` shows running)
- Agent registered and running (`ax gateway agents show <agent>`)
- You are a member of the target space

## Steps

### 1. Check current space binding

```bash
ax gateway agents show dev-sentinel
```

Note the `active_space_name` and `active_space_id` fields. This is where the
agent currently operates.

### 2. List available spaces

```bash
ax spaces list
```

Find the target space name or ID.

### 3. Switch the agent's space

```bash
ax spaces use <target-space-name>
```

This updates the active space in `session.json`. The reconcile loop (runs every
~10 seconds) will pick up the change and rebind the agent.

### 4. Verify the switch

```bash
ax gateway agents show dev-sentinel
```

**Expected:** `active_space_name` shows the new space name. `active_space_id`
shows the new space UUID.

### 5. Test message delivery

```bash
ax send "space switch test" --to dev-sentinel --skip-ax
ax gateway agents inbox dev-sentinel
```

The message should appear in the inbox under the new space context.

## Verify

- `agents show` displays the new space name (not a UUID)
- Messages route correctly in the new space
- The agent's credentials remain valid (no re-registration needed)

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| `active_space_name` shows a UUID | Space cache has UUID-as-name from upstream | Wait for cache refresh, or restart Gateway to force a fresh `list_spaces` |
| Agent stops responding after switch | Space binding not propagated | Check `session.json` for the new `space_id`, then `ax gateway agents restart <agent>` |
| "Not a member of space" error | Your user PAT doesn't have access to the target space | Ask admin to add you to the space |

## Learning goal

Understanding the space resolution cascade: how `session.json`, per-agent
`allowed_spaces` cache, and upstream API work together when an operator
changes spaces. See [Gateway Agent Runtimes — Space Resolution](../gateway-agent-runtimes.md#space-resolution).
