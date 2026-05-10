# Scenario: Debug a Stuck Agent

## Goal

Diagnose why an agent is registered but not processing messages.

## Prerequisites

- Gateway running
- Agent registered (`ax gateway agents show <agent>` returns data)

## Steps

### 1. Check agent state

```bash
ax gateway agents show dev-sentinel --json
```

Look at three fields:

| Field | Healthy value | Problem values |
| --- | --- | --- |
| `desired_state` | `running` | `stopped` — operator or system stopped it |
| `effective_state` | `running` | `stopped`, `error`, `pending_approval` |
| `presence` | `online` | `offline`, `stale` |

If `desired_state` is `stopped`, the agent was intentionally stopped. Start it:

```bash
ax gateway agents start dev-sentinel
```

### 2. Check Gateway status

```bash
ax gateway status --json
```

Verify Gateway itself is running and healthy. Check `uptime`, `agent_count`,
and `error_count`.

### 3. Check the reconcile loop

The reconcile loop runs every ~10 seconds. If the agent's `effective_state`
doesn't match `desired_state`, the loop should fix it automatically.

Wait 30 seconds and re-check:

```bash
ax gateway agents show dev-sentinel
```

If the state hasn't changed, the reconcile loop may be stuck or erroring.

### 4. Check Gateway logs

```bash
cat ~/.ax/gateway/gateway.log | tail -50
```

Look for error lines mentioning your agent name. Common patterns:

- `RuntimeError: ... token expired` — managed credential needs refresh
- `ConnectionError: ... connection refused` — upstream API unreachable
- `ValueError: ... space_id not found` — space binding is stale

### 5. Check activity log

```bash
cat ~/.ax/gateway/activity.jsonl | grep dev-sentinel | tail -10
```

The activity log shows timestamped events per agent. Look for the last
`picked_up`, `working`, `completed`, or `error` signal.

### 6. Force restart if needed

```bash
ax gateway agents stop dev-sentinel
ax gateway agents start dev-sentinel
```

Then verify:

```bash
ax gateway agents show dev-sentinel
```

## Verify

- `effective_state` is `running`
- `presence` is `online`
- Send a test message and confirm it appears in the inbox

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| Agent stuck in `pending_approval` | Pass-through agent awaiting operator approval | Open <http://127.0.0.1:8765/operator> and approve the pending agent row |
| Agent shows `error` state | Runtime crashed or token invalid | Check `gateway.log` for the error, then restart |
| Reconcile loop not fixing state | Gateway daemon itself is unhealthy | `ax gateway stop && ax gateway start` |
| Agent online but not receiving messages | Wrong space binding | Verify `active_space_id` matches the space where messages are being sent |

## Learning goal

Understanding the three-layer agent state model: desired state (what the
operator wants), effective state (what Gateway observes), and presence (what
the upstream platform reports). The reconcile loop bridges desired to effective;
upstream presence is informational. See [Gateway Agent Runtimes — Agent Lifecycle](../gateway-agent-runtimes.md#agent-lifecycle).
