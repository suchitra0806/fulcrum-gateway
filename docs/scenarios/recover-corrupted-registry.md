# Scenario: Recover From a Corrupted Registry

## Goal

Restore Gateway to a working state after `registry.json` becomes corrupted or
inconsistent.

## Prerequisites

- Gateway stopped or erroring on start
- Access to the Gateway state directory (`~/.ax/gateway/`)

## Steps

### 1. Stop Gateway

```bash
ax gateway stop
```

If Gateway won't stop cleanly:

```bash
# Find the PID
cat ~/.ax/gateway/gateway.pid

# Kill it
kill $(cat ~/.ax/gateway/gateway.pid)
```

### 2. Inspect the registry

```bash
cat ~/.ax/gateway/registry.json | python3 -m json.tool
```

If this fails with a JSON parse error, the file is corrupted (truncated write,
disk full, etc.).

Common corruption patterns:

| Pattern | Cause | What you see |
| --- | --- | --- |
| Truncated JSON | Write interrupted (crash, disk full) | `Expecting ',' delimiter` parse error |
| Missing agent entries | Concurrent writes from multiple processes | Agent registered but not in file |
| Stale gateway block | Old space_id/space_name not migrated | UUID where a space name should be |
| Duplicate agent entries | Bug in registration logic | Same agent name appears twice |

### 3. Back up the corrupted file

```bash
cp ~/.ax/gateway/registry.json ~/.ax/gateway/registry.json.corrupted.$(date +%s)
```

### 4. Attempt auto-repair

Start Gateway — it runs auto-migration and corruption repair on startup:

```bash
ax gateway start
```

Check the log for repair messages:

```bash
grep -i "repair\|migrate\|corrupt" ~/.ax/gateway/gateway.log | tail -10
```

### 5. If auto-repair fails: manual repair

If the JSON is unparseable, you have two options:

**Option A: Edit the JSON.** Fix the syntax error manually. Common fixes:
- Add a missing closing `}` or `]`
- Remove a trailing comma before `}`
- Remove duplicate entries

**Option B: Start fresh.** Remove the registry and re-register agents:

```bash
mv ~/.ax/gateway/registry.json ~/.ax/gateway/registry.json.backup.$(date +%s)
ax gateway start
```

Then re-register each agent:

```bash
ax gateway agents add dev-sentinel --template hermes --workdir ~/agents/dev-sentinel
ax gateway agents add echo-bot --template echo
# ... repeat for each agent
```

### 6. Also check session.json

```bash
cat ~/.ax/gateway/session.json | python3 -m json.tool
```

If session.json is also corrupted, remove it — it contains only ephemeral
runtime state (active spaces, session tokens) that will be rebuilt:

```bash
mv ~/.ax/gateway/session.json ~/.ax/gateway/session.json.backup.$(date +%s)
```

### 7. Verify recovery

```bash
ax gateway start
ax gateway status
ax gateway agents list
```

All agents should appear. Start any that aren't running:

```bash
ax gateway agents start dev-sentinel
```

Test message delivery:

```bash
ax send "recovery test" --to echo-bot --skip-ax
```

## Verify

- Gateway starts without errors
- `registry.json` is valid JSON
- All expected agents appear in `agents list`
- Message delivery works end-to-end

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| Agent credentials lost | Token files in `~/.ax/gateway/agents/<name>/token` were deleted | Re-register the agent — Gateway will mint a new managed credential |
| Agents have wrong space after recovery | `session.json` was rebuilt with default space | Re-run `ax spaces use <space>` to rebind |
| Gateway starts but agents won't | Stale PID files in agent directories | Delete `~/.ax/gateway/agents/<name>/*.pid` |

## Learning goal

Understanding Gateway's state files and what each one stores:

| File | Contents | Recreatable? |
| --- | --- | --- |
| `registry.json` | Agent registrations, templates, workdirs, credential refs | Only by re-registering agents |
| `session.json` | Active spaces, session tokens, presence | Yes — rebuilt on start |
| `agents/<name>/token` | Managed agent credentials | Only by re-minting |
| `agents/<name>/pending.json` | Unread message queue | Lost messages are lost |
| `gateway.pid` | Daemon process ID | Deleted on clean shutdown |
| `gateway.log` | Daemon logs | Informational only |
| `activity.jsonl` | Agent activity events | Informational only |

See [ADR-004](../adr/ADR-004-space-state-in-session.md) for why space state
lives in `session.json` and not `registry.json`.
